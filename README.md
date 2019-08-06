# lambda-spot-interruption

Lambda function used resize a auto scaling group based on spot interruption warning.

This lambda function receives a CloudWatch event informing that a instance is going
to be removed. With the account id and instance id it drains the instance from the
target group or from load balancer and, optionally, increases by one the desired count of
another auto scaling group.

This lambda function was created with multi account environments in mind, the function
runs with a [role](assume-policy.json) which only has the permission to assume other [roles](policy.json).

The account that is running this lambda function needs to enable other accounts to send events
to its [event bus](https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#eventbuses:), the other accounts needs to be configured to
[route](https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#rules:action=create) the interruption warning to the main account and add
the [role](policy.json) to enable the main account to perform the required actions.

sample event:
```
{
  "version": "0",
  "id": "1e5527d7-bb36-4607-3370-4164db56a40e",
  "detail-type": "EC2 Spot Instance Interruption Warning",
  "source": "aws.ec2",
  "account": "123456789012",
  "time": "1970-01-01T00:00:00Z",
  "region": "us-east-1",
  "resources": [
    "arn:aws:ec2:us-east-1b:instance/i-0b662ef9931388ba0"
  ],
  "detail": {
    "instance-id": "i-0b662ef9931388ba0",
    "instance-action": "terminate"
  }
}
```

#### Dependencies

* Python 3.7

```
curl -o python3.7.tar.gz https://www.python.org/ftp/python/3.7.4/Python-3.7.4.tgz
tar xvf python3.7.tar.gz
cd Python-3.7.4/
./configure --enable-optimizations --with-ensurepip=install
sudo make altinstall -j $(nproc)
```

* AWS Role
###### On all accounts
* Create a role, import the [policy](policy.json).
* Route the spot [interruption warning](https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#rules:action=create) to the main account.

###### On main account
* Create a role and import the [assume-policy](assume-policy.json)
* Allow other accounts to send events to this account [event bus](https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#eventbuses:).

#### Configuration

Configuration is done through environment variables defined inside the Makefile

| Variable   	    | Description                                                                       	| Requirement 	| Default Value           	                                    |
|---------------	|-----------------------------------------------------------------------------------	|-------------	|-------------------------------------------------------------	|
| LAMBDA\_ROLE 	    | Name of the role to be used by the lambda function                                	| YES         	|                                                               |
| ROLE\_NAME 	    | Name of the role to be assumed by the lambda function                                	| YES         	| lambda-spot-interruption                                      |
| TAGS        	    | Lambda function tags                                                              	| Optional    	|                         	                                    |
| FUNCTION\_NAME    | Lambda function name                                                              	| Optional    	| letsencrypt\_internal   	                                    |
| DESCRIPTION 	    | Lambda function description                                                       	| Optional    	| Lambda function used to provision letsencrypt certificates    |
| REGION      	    | Region to deploy the lambda function                                              	| Optional    	| us-east-1               	                                    |
| ZIP\_FILE    	    | Name of the compressed environment file                                           	| Optional    	| lambda-spot-interruption.zip                                  |
| MEMORY\_SIZE	    | Maximum memory available to the lambda function                                   	| Optional    	| 192                     	                                    |

#### Create an isolated environment

`make dependencies`

#### Compress the environment

`make pack`

#### Create lambda function

`make create-function LAMBDA_ROLE='arn:aws:iam::_ACCOUNT_ID1:role/lambda-spot-interruption-assume' TAGS='name1=value1,name2=value2' ROLE_NAME='lambda-spot-interruption'`

#### Recreate the isolated environment, compress and upload

`make deploy`
