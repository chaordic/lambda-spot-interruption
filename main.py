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
                 agg_gw,
                 session=boto3):
        self.account_id = account_id
        self.role_name = role_name
        self.region = region
        self.instance_id = instance_id

        self.session = self.assume_role(session)

        self.alb = self.session.client('elbv2')
        self.elb = self.session.client('elb')
        self.ec2 = self.session.client('ec2')
        self.asg = self.session.client('autoscaling')

        self.current_asg = self.get_current_asg()

        if self.current_asg is None:
            raise ValueError

        self.target_asg_name, self.target_asg_opts, self.target_asg = self.get_desired_asg(
        )
        self.lb_type, self.resource_id = self.find_tg()

        self.agg_gw = agg_gw
        self.prefix = 'lambda_spot_interruption_'

        self.metric(name='termination', reason='termination')

    def metric(self, name, reason, extra_labels=None, value=1):
        if self.agg_gw is None:
            return

        if self.resource_id is None:
            tg_name = 'could not find'
        else:
            tg_name = self.resource_id.split('/')[1]

        labels = f'account_id="{self.account_id}",asg="{self.current_asg}",lb_type="{self.lb_type}",tg="{tg_name}",reason="{reason}"'
        if extra_labels is not None:
            labels += f',{extra_labels}'

        metric = f"{self.prefix}{name}{{{labels}}} {value}\n"
        try:
            req = requests.post(
                self.agg_gw,
                data=metric,
                headers={'Content-Type': 'text/xml'},
                timeout=2)
        except Timeout:
            print('Timed out while trying to post metrics!')

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
        try:
            current_asg = self.ec2.describe_tags(
                Filters=[{
                    'Name': 'resource-id',
                    'Values': [self.instance_id]
                }, {
                    'Name': 'key',
                    'Values': ['aws:autoscaling:groupName']
                }])['Tags'][0]['Value']
        except IndexError:
            print(f"Could not find ASG for instance {self.instance_id}")
            return None

        print(
            f"Found current ASG tag {current_asg} for instance {self.instance_id}"
        )
        return current_asg

    def get_desired_asg(self):
        tags = self.ec2.describe_tags(Filters=[{
            'Name': 'resource-id',
            'Values': [self.instance_id]
        }, {
            'Name': 'key',
            'Values': ['asgOnDemand']
        }])
        try:
            targetAsg = tags['Tags'][0]['Value']
        except (KeyError, IndexError):
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
    agg_gw = os.getenv('AGG_GW',
                       None)  # weaveworks prom-aggregation-gateway endpoint

    print(
        f"Instance {instance_id} in account {account_id} in region {region} is going down"
    )

    try:
        spot = Spot(account_id, region, role_name, instance_id, agg_gw)

        # Drain the target group or load balancer if configured
        spot.drain_from_lb()

        if spot.target_asg_name is None:
            print(
                f"Unable to describe tags or find the desired ASG for instance id: {instance_id}"
            )
            return

        # increase ASG size
        spot.resize_asg()

    except ValueError:
        # could not even find the current asg, aborting
        return


# simulate the event locally
if __name__ == '__main__':
    with open('event.json') as f:
        data = json.load(f)
        data['account'] = os.environ['ACCOUNT']
        handler(data, None)
