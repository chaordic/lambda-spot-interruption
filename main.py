#!/usr/bin/env python3
import json
import os
import re
import requests
import boto3

from requests.exceptions import Timeout


class Spot():
    def __init__(self,
                 account_id,
                 region,
                 role_name,
                 instance_id,
                 metrics_namespace,
                 session=boto3):
        self.account_id = account_id
        self.role_name = role_name
        self.region = region
        self.instance_id = instance_id

        self.cw = boto3.client('cloudwatch')

        self.session = self.assume_role(session)

        self.alb = self.session.client('elbv2')
        self.elb = self.session.client('elb')
        self.ec2 = self.session.client('ec2')
        self.asg = self.session.client('autoscaling')

        self.metrics_namespace = metrics_namespace
        self.prefix = 'lambda_spot_interruption_'

        self.instance_name, self.current_asg = self.get_current_asg()

        self.target_asg_name, self.target_asg_opts, self.target_asg = self.get_desired_asg(
        )
        self.lb_type, self.resource_id = self.find_tg()

        self.metric(name='termination', reason='termination')

    def metric(self, name, reason, value=1):
        if self.metrics_namespace is None:
            return

        if self.current_asg is None:
            metric_name = 'InstanceName'
            metric_value = self.instance_name
        else:
            metric_name = 'AutoScaleGroup'
            metric_value = self.current_asg

        self.cw.put_metric_data(
            Namespace=self.metrics_namespace,
            MetricData=[{
                'MetricName':
                name,
                'Value':
                value,
                'Unit':
                'Count',
                'Dimensions': [{
                    'Name': metric_name,
                    'Value': metric_value
                }, {
                    'Name': 'AccountID',
                    'Value': self.account_id
                }, {
                    'Name': 'Status',
                    'Value': reason
                }]
            }])

    def assume_role(self, session):
        arn = f"arn:aws:iam::{self.account_id}:role/{self.role_name}"
        print(f"Trying to assume role {arn}")
        stsclient = session.client('sts')

        assumed_role_object = stsclient.assume_role(
            RoleArn=arn, RoleSessionName='LambdaAssumeRole')

        credentials = assumed_role_object['Credentials']

        session = session.session.Session(
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'])

        print("Successfully assumed role")
        return session

    def get_current_asg(self):
        self.tags = self.ec2.describe_tags(
            Filters=[{
                'Name': 'resource-id',
                'Values': [self.instance_id]
            }])['Tags']

        self.current_asg = next((tag['Value'] for tag in self.tags
                                 if tag['Key'] == 'aws:autoscaling:groupName'),
                                None)
        self.instance_name = next(
            (tag['Value'] for tag in self.tags if tag['Key'] == 'Name'), None)

        if self.current_asg is None:
            print(f"Could not find ASG for instance {self.instance_id}")
            self.metric(name='fail', reason='Could not find ASG')

        return self.instance_name, self.current_asg

    def get_desired_asg(self):
        targetAsg = next(
            (tag['Value'] for tag in self.tags if tag['Key'] == 'asgOnDemand'),
            None)

        if targetAsg is None:
            self.metric(name='fail', reason='missing target asg')
            return None, None, None

        target_asg_name, *target_asg_opts = targetAsg.split(';')
        target_asg = self.asg.describe_auto_scaling_groups(
            AutoScalingGroupNames=[target_asg_name])
        target_asg = target_asg['AutoScalingGroups'][0]
        print(
            f"Found ASG tag {target_asg_name} with options {target_asg_opts}")
        return target_asg_name, target_asg_opts, target_asg

    def find_tg(self):
        if self.current_asg is None:
            return None, None

        tgs = self.asg.describe_load_balancer_target_groups(
            AutoScalingGroupName=self.current_asg)['LoadBalancerTargetGroups']

        if len(tgs) == 0:
            # Searching Classic LB
            allLBs = self.elb.describe_load_balancers()

            for lb in allLBs['LoadBalancerDescriptions']:
                for instance in lb['Instances']:
                    if instance['InstanceId'] == self.instance_id:
                        print(f"Found Classic LB {lb['LoadBalancerName']}")
                        return 'elb', lb['LoadBalancerName']
        else:
            # Searching ALB
            for tg in tgs:
                tgArn = tg['LoadBalancerTargetGroupARN']
                #tgName = tg['TargetGroupName']
                tgHealth = self.alb.describe_target_health(
                    TargetGroupArn=tgArn)

                for instance in tgHealth['TargetHealthDescriptions']:
                    if instance['Target']['Id'] == self.instance_id:
                        print(f"Found TG {tgArn}")
                        return 'alb', tgArn

        print(f"Unable to find a LB with instance id: {self.instance_id}")
        return None, None

    def drain_from_lb(self):
        if self.lb_type == 'alb':
            # drain from the target group
            deregisterTargets = self.alb.deregister_targets(
                TargetGroupArn=self.resource_id,
                Targets=[{
                    'Id': self.instance_id
                }])

        elif self.lb_type == 'elb':
            # drain from the LB
            self.elb.deregister_instances_from_load_balancer(
                LoadBalancerName=self.resource_id,
                Instances=[{
                    'InstanceId': self.instance_id
                }])
        else:
            return

        self.metric(name='drain', reason='drain')
        print(
            f"Draining instance {self.instance_id} from {self.lb_type} {self.resource_id}"
        )

    def resize_asg(self, count=1):
        # get all options, if any
        opts = re.findall(r'[\w:]+\s*=\s*[\w:]+',
                          ';'.join(self.target_asg_opts))

        # split by '='
        opts = dict([m.split('=', 1) for m in opts])

        # check if DesiredCapacity is bigger than the MaxDesired custom tag, if it
        # does not exists than check with ASG MaxSize
        maxSize = int(opts.get('MaxDesired', self.target_asg['MaxSize']))

        # error if we are already at max capacity
        if self.target_asg['DesiredCapacity'] >= maxSize:
            print("Auto scaling group already at max size!")
            print(
                f"Current: {self.target_asg['DesiredCapacity']}\nMax: {maxSize}"
            )
            self.metric(name='fail', reason='Already at max size')
            return False

        self.target_asg['DesiredCapacity'] += count
        print(
            f"Resizing ASG {self.target_asg['AutoScalingGroupName']} to desired capacity {self.target_asg['DesiredCapacity']}"
        )
        self.asg.update_auto_scaling_group(
            AutoScalingGroupName=self.target_asg['AutoScalingGroupName'],
            DesiredCapacity=self.target_asg['DesiredCapacity'])

        self.metric(name='scale', reason='scale')
        return True


def handler(event, context):

    instance_id = event['detail']['instance-id']
    account_id = event['account']
    region = event['region']
    role_name = os.environ['ROLE_NAME']
    metrics_namespace = os.getenv('CW_METRICS_NAMESPACE', None)

    print(
        f"Instance {instance_id} in account {account_id} in region {region} is going down"
    )

    spot = Spot(account_id, region, role_name, instance_id, metrics_namespace)

    # Drain the target group or load balancer if configured
    spot.drain_from_lb()

    if spot.target_asg_name is None:
        print(
            f"Unable to describe tags or find the desired ASG for instance id: {instance_id}"
        )
        return

    # increase ASG size
    spot.resize_asg()


# simulate the event locally
if __name__ == '__main__':
    with open('event.json') as f:
        data = json.load(f)
        data['account'] = os.environ['ACCOUNT']
        handler(data, None)
