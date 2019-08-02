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
            targetAsg['AutoScalingGroupName'], targetAsg['DesiredCapacity']))

        # increase desired capacity
        targetAsg['DesiredCapacity'] = targetAsg['DesiredCapacity'] + 1

        asgclient.update_auto_scaling_group(
            AutoScalingGroupName=targetAsg['AutoScalingGroupName'],
            DesiredCapacity=targetAsg['DesiredCapacity'])

    except ValueTooBig:
        raise ValueTooBig


def findTg(elbv2client, elbclient, instanceId):
    try:
        # Searching ELBv2
        allTGs = elbv2client.describe_target_groups()

        for tg in allTGs['TargetGroups']:
            tgArn = tg['TargetGroupArn']
            tgName = tg['TargetGroupName']
            tgHealth = elbv2client.describe_target_health(TargetGroupArn=tgArn)

            for instance in tgHealth['TargetHealthDescriptions']:
                if instance['Target']['Id'] == instanceId:
                    print("Found TG {}".format(tgName))
                    return tgArn, tgName

        # Searching Classic LB
        allLBs = elbclient.describe_load_balancers()

        for lb in allLBs['LoadBalancerDescriptions']:
            for instance in lb['Instances']:
                if instance['InstanceId'] == instanceId:
                    print("Found Classic LB {}".format(lb))
                    return None, lb['LoadBalancerName']
        raise ValueNotFound
    except ValueNotFound:
        raise ValueNotFound


def drainFromLb(elbv2client, elbclient, instanceId):
    try:
        tgArn, tgName = findTg(elbv2client, elbclient, instanceId)

        # Application LB
        if tgArn != None:
            print("Draining instance {} on TG {}".format(instanceId, tgName))

            # drain from the target group
            deregisterTargets = elbv2client.deregister_targets(
                TargetGroupArn=tgArn, Targets=[{
                    'Id': instanceId
                }])

            return tgName

        # Classic LB
        elif tgName != None:
            print("Draining instance {} on LB {}".format(instanceId, tgName))

            # drain from the LB
            elbclient.deregister_instances_from_load_balancer(
                LoadBalancerName=tgName, Instances=[instanceId])

            return tgName

        else:
            raise ValueNotFound
    except ValueNotFound:
        raise ValueNotFound


def getDesiredAsg(ec2client, instanceId):
    try:
        # get the ASG that we should increase
        targetAsg = ec2client.describe_tags(Filters=[{
            'Name': 'resource-id',
            'Values': [instanceId],
            'Name': 'key',
            'Values': ['asgOnDemand']
        }])['Tags'][0]['Value']

        print("Found ASG tag {}".format(targetAsg))
        return targetAsg

    except:
        raise ValueNotFound


def assumeRole(account, role):
    arn = "arn:aws:iam::{}:role/{}".format(account, role)
    try:
        stsclient = boto3.client('sts')
        assumed_role_object = stsclient.assume_role(
            RoleArn=arn, RoleSessionName='AssumeRoleACM')
        credentials = assumed_role_object['Credentials']
        session = boto3.session.Session(
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'])

        return session
    except:
        raise


def handler(event, context):
    instanceId = event['detail']['instance-id']
    accountNumber = event['account']
    region = event['region']
    roleName = os.environ['ROLE_NAME']

    try:
        session = assumeRole(accountNumber, roleName)
    except:
        print("Failed to assume role")
        return

    print("Instance {} in account {} in region {} is going down".format(
        instanceId, accountNumber, region))

    # find if this instance is configured in a TG and drain it if it is
    try:
        elbv2client = session.client('elbv2')
        elbclient = session.client('elb')

        targetTg = drainFromLb(elbv2client, elbclient, instanceId)
    except ValueNotFound:
        errMsg = "Unable to find a TG with instance id: {}".format(instanceId)
        print(errMsg)
    else:
        print("Draining instance {} from TG {}".format(instanceId, targetTg))

    # find the desired ASG to resize
    try:
        ec2client = session.client('ec2')

        targetAsg = getDesiredAsg(ec2client, instanceId)

    except ValueNotFound:
        errMsg = "Unable to describe tags or find the desired ASG for instance id: {}".format(
            instanceId)
        print(errMsg)
        return

    # increase ASG size
    try:
        asgclient = session.client('autoscaling')

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
        handler(data, None)
