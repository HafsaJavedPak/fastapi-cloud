"""
This code is a unit test suite using pytest and Python's built-in unittest.mock library to test the AWS Lambda function in lambda_src/nginx_sync.py.

Because the Lambda interacts with AWS services (Auto Scaling, EC2, SSM), running real tests would require a live AWS environment, which is slow and costs money. Instead, this suite uses "mocking" (via MagicMock and patch) to intercept the boto3 API calls and return fake AWS responses. This allows us to instantly verify the script's core logic—like formatting the Nginx config correctly and safely ignoring servers that are shutting down—without making a single network request.
"""

from unittest.mock import MagicMock, patch  # Imports tools to create fake objects (Mocks) and intercept functions (patch) so we don't hit real AWS APIs
import os  # Imports the os module to set up dummy environment variables for our tests
import pytest  # Imports the pytest testing framework

os.environ.setdefault("ASG_NAME", "fastapi-demo-asg")  # Sets a fake Auto Scaling Group name in the environment variables for the Lambda to read
os.environ.setdefault("PROXY_INSTANCE_ID", "i-proxy123")  # Sets a fake Nginx proxy instance ID in the environment variables

from lambda_src import nginx_sync  # Imports the actual Lambda function code (assuming it's saved in a file/folder named lambda_src/nginx_sync.py)


def _clients(instances, reservations):  # Defines a helper function to quickly generate fake AWS (boto3) clients with pre-programmed responses
    asg, ec2, ssm = MagicMock(), MagicMock(), MagicMock()  # Creates three "MagicMock" objects which will pretend to be our boto3 clients
    asg.describe_auto_scaling_groups.return_value = {  # Programs the fake ASG client to return a specific dictionary when asked to describe groups
        "AutoScalingGroups": [{"Instances": instances}]  # Injects the 'instances' argument into the fake AWS response structure
    }  # Closes the fake ASG response dictionary
    ec2.describe_instances.return_value = {"Reservations": reservations}  # Programs the fake EC2 client to return the provided 'reservations' (IP addresses)
    ssm.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}  # Programs the fake SSM client to pretend a command was sent successfully and returned ID 'cmd-123'
    return {"autoscaling": asg, "ec2": ec2, "ssm": ssm}  # Returns a dictionary mapping AWS service names to our programmed fake clients


def test_builds_upstream_from_two_instances():  # Defines a test for the "happy path" where we have two healthy backend servers
    clients = _clients(  # Calls our helper to set up the fake AWS data
        [  # Provides a list of two fake instances attached to the ASG
            {"InstanceId": "i-1", "LifecycleState": "InService", "HealthStatus": "Healthy"},  # Fake instance 1, marked as healthy and ready
            {"InstanceId": "i-2", "LifecycleState": "InService", "HealthStatus": "Healthy"},  # Fake instance 2, marked as healthy and ready
        ],  # Closes the ASG instances list
        [{"Instances": [{"PrivateIpAddress": "10.0.2.11"}, {"PrivateIpAddress": "10.0.2.12"}]}],  # Provides the fake EC2 response containing their IP addresses
    )  # Closes the _clients helper call
    with patch.object(nginx_sync.boto3, "client", side_effect=lambda s: clients[s]):  # Temporarily overrides boto3.client inside our Lambda to return our fake clients instead of making real AWS calls
        result = nginx_sync.handler({}, None)  # Runs the Lambda handler function with an empty event and context
    assert result["status"] == "updated"  # Verifies the Lambda returned a status saying it updated the config
    assert result["instances"] == ["10.0.2.11", "10.0.2.12"]  # Verifies both fake IP addresses were correctly processed and included
    cmd = clients["ssm"].send_command.call_args.kwargs["Parameters"]["commands"][0]  # Extracts the exact bash command the Lambda tried to send to SSM
    assert "server 10.0.2.11:8000" in cmd and "server 10.0.2.12:8000" in cmd  # Verifies the generated Nginx config block contains both fake IP addresses on port 8000
    assert "nginx -t && nginx -s reload" in cmd  # Verifies the bash command includes the instructions to safely test and reload Nginx


def test_skips_terminating_instances():  # Defines a test to ensure servers that are shutting down are safely ignored
    clients = _clients(  # Calls our helper to set up fake AWS data
        [  # Provides a list of fake ASG instances
            {"InstanceId": "i-1", "LifecycleState": "InService", "HealthStatus": "Healthy"},  # Instance 1 is healthy and running
            {"InstanceId": "i-2", "LifecycleState": "Terminating", "HealthStatus": "Healthy"},  # Instance 2 is shutting down (Terminating)
        ],  # Closes the ASG list
        [{"Instances": [{"PrivateIpAddress": "10.0.2.11"}]}],  # Provides EC2 details for only the first instance (since the second should be filtered out)
    )  # Closes the _clients call
    with patch.object(nginx_sync.boto3, "client", side_effect=lambda s: clients[s]):  # Intercepts boto3 calls to inject our fake clients
        result = nginx_sync.handler({}, None)  # Runs the Lambda function
    assert result["instances"] == ["10.0.2.11"]  # Verifies the Lambda only returned the IP of the healthy instance, successfully ignoring the terminating one
    clients["ec2"].describe_instances.assert_called_once_with(InstanceIds=["i-1"])  # Double-checks that the Lambda only asked EC2 for the IP of instance i-1


def test_no_healthy_instances_keeps_upstream():  # Defines a test for when all backend servers are down
    clients = _clients([], [])  # Sets up fake AWS clients returning completely empty lists (no instances running)
    with patch.object(nginx_sync.boto3, "client", side_effect=lambda s: clients[s]):  # Intercepts boto3 calls
        result = nginx_sync.handler({}, None)  # Runs the Lambda function
    assert result["status"] == "no_change"  # Verifies the Lambda detected the empty list and aborted safely, returning "no_change"
    clients["ssm"].send_command.assert_not_called()  # Proves that no command was sent to Nginx (safeguarding the existing config from being accidentally wiped out)


def test_generated_conf_is_valid_nginx_shape():  # Defines a test that directly checks the helper function that builds the Nginx config string
    conf = nginx_sync._build_conf(["10.0.2.11"])  # Calls the helper function directly with a single fake IP
    assert conf.startswith("upstream fastapi_backend {")  # Verifies the generated string correctly starts the Nginx upstream block
    assert conf.rstrip().endswith("}")  # Verifies the generated string (after stripping trailing newlines) ends with a closing brace
    assert conf.count("{") == conf.count("}")  # Counts the curly braces to ensure they are perfectly balanced (so Nginx doesn't crash on a syntax error)
