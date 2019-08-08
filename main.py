#!/usr/bin/env python3
import json
import boto3
import os


# define Python user-defined exceptions
class Error(Exception):
    """Base class for other exceptions"""
    pass


class ValueNotFound(Error):
    """Raised when the requested value could not be found"""
    pass


class ValueTooBig(Error):
    """Raised when the desired capacity is bigger than the maxium limit"""
    pass


def resizeAsg(asgclient, asgName):
    try:
        # get target ASG
        targetAsg = asgclient.describe_auto_scaling_groups(
            AutoScalingGroupNames=[asgName])['AutoScalingGroups'][0]

        # error if we are already at max capacity
        if targetAsg['DesiredCapacity'] >= targetAsg['MaxSize']:
            print("Auto scaling group already at max size!")
            raise ValueTooBig

        print("Resizing ASG {} to desired capacity {}".format(
            targetAsg['AutoScalingGroupName'],
            int(targetAsg['DesiredCapacity']) + 1))

        # increase desired capacity
        targetAsg['DesiredCapacity'] = targetAsg['DesiredCapacity'] + 1

        asgclient.update_auto_scaling_group(
            AutoScalingGroupName=targetAsg['AutoScalingGroupName'],
            DesiredCapacity=targetAsg['DesiredCapacity'])

    except ValueTooBig:
        raise ValueTooBig


def findTg(elbv2client, elbclient, asgclient, currentAsg, instanceId):
    try:
        tgs = asgclient.describe_load_balancer_target_groups(
            AutoScalingGroupName=currentAsg)['LoadBalancerTargetGroups']

        if len(tgs) == 0:
            # Searching Classic LB
            allLBs = elbclient.describe_load_balancers()

            for lb in allLBs['LoadBalancerDescriptions']:
                for instance in lb['Instances']:
                    if instance['InstanceId'] == instanceId:
                        print("Found Classic LB {}".format(
                            lb['LoadBalancerName']))
                        return 'elb', lb['LoadBalancerName']
        else:
            # Searching ELBv2
            for tg in tgs:
                tgArn = tg['LoadBalancerTargetGroupARN']
                #tgName = tg['TargetGroupName']
                tgHealth = elbv2client.describe_target_health(
                    TargetGroupArn=tgArn)

                for instance in tgHealth['TargetHealthDescriptions']:
                    if instance['Target']['Id'] == instanceId:
                        print("Found TG {}".format(tgArn))
                        return 'elbv2', tgArn

        raise ValueNotFound
    except ValueNotFound:
        raise ValueNotFound


def drainFromLb(elbv2client, elbclient, ec2client, asgclient, instanceId):
    try:
        currentAsg = getCurrentAsg(ec2client, instanceId)
        elbType, resourceId = findTg(elbv2client, elbclient, asgclient,
                                     currentAsg, instanceId)

        # Application LB
        if elbType == 'elbv2':
            # drain from the target group
            deregisterTargets = elbv2client.deregister_targets(
                TargetGroupArn=resourceId, Targets=[{
                    'Id': instanceId
                }])

            return 'ELBv2', resourceId

        # Classic LB
        elif elbType == 'elb':
            # drain from the LB
            elbclient.deregister_instances_from_load_balancer(
                LoadBalancerName=resourceId,
                Instances=[{
                    'InstanceId': instanceId
                }])

            return 'ELB Classic', resourceId

        else:
            raise ValueNotFound
    except ValueNotFound:
        raise ValueNotFound


def getCurrentAsg(ec2client, instanceId):
    try:
        # get the ASG that we should increase
        currentAsg = ec2client.describe_tags(
            Filters=[{
                'Name': 'resource-id',
                'Values': [instanceId]
            }, {
                'Name': 'key',
                'Values': ['aws:autoscaling:groupName']
            }])['Tags'][0]['Value']

        print("Found current ASG tag {} for instance {}".format(
            currentAsg, instanceId))
        return currentAsg

    except:
        raise ValueNotFound


def getDesiredAsg(ec2client, instanceId):
    try:
        # get the ASG that we should increase
        targetAsg = ec2client.describe_tags(Filters=[{
            'Name': 'resource-id',
            'Values': [instanceId]
        }, {
            'Name': 'key',
            'Values': ['asgOnDemand']
        }])['Tags'][0]['Value']

        print("Found ASG tag {}".format(targetAsg))
        return targetAsg

    except:
        raise ValueNotFound


def assumeRole(account, role, session):
    arn = "arn:aws:iam::{}:role/{}".format(account, role)
    print("Trying to assume role {}".format(arn))
    try:
        stsclient = session.client('sts')
        assumed_role_object = stsclient.assume_role(
            RoleArn=arn, RoleSessionName='AssumeRoleACM')
        credentials = assumed_role_object['Credentials']
        session = session.session.Session(
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'])

        print("Successfully assumed role")
        return session
    except:
        raise


def handler(event, context, session=boto3):
    instanceId = event['detail']['instance-id']
    accountNumber = event['account']
    region = event['region']
    roleName = os.environ['ROLE_NAME']

    try:
        session = assumeRole(accountNumber, roleName, session)
    except Exception as e:
        print("Failed to assume role. {}".format(e))
        return

    print("Instance {} in account {} in region {} is going down".format(
        instanceId, accountNumber, region))

    # find if this instance is configured in a TG and drain it if it is
    try:
        elbv2client = session.client('elbv2')
        elbclient = session.client('elb')
        ec2client = session.client('ec2')
        asgclient = session.client('autoscaling')

        elbType, resourceId = drainFromLb(elbv2client, elbclient, ec2client,
                                          asgclient, instanceId)
    except ValueNotFound:
        errMsg = "Unable to find a LB with instance id: {}".format(instanceId)
        print(errMsg)
    else:
        print("Draining instance {} from {} {}".format(instanceId, elbType,
                                                       resourceId))

    # find the desired ASG to resize
    try:
        targetAsg = getDesiredAsg(ec2client, instanceId)

    except ValueNotFound:
        errMsg = "Unable to describe tags or find the desired ASG for instance id: {}".format(
            instanceId)
        print(errMsg)
        return

    # increase ASG size
    try:
        resizeAsg(asgclient, targetAsg)
    except ValueTooBig:
        print(
            "Unable to resize auto scaling group {}, already at max capacity".
            format(targetAsg))
        return


# simulate the event locally
if __name__ == '__main__':
    with open('event.json') as f:
        data = json.load(f)
        account = os.environ['ACCOUNT']
        roleName = os.environ['ROLE_NAME']
        session = assumeRole(account, roleName + '-assume', boto3)
        handler(data, None, session)
