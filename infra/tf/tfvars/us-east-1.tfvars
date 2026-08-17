region = "us-east-1"

# Must already exist in AWS (Terraform doesn't manage the key pair, so no
# private key material ever touches state) - see the manual steps for how
# to create it.
key_name = "sawalha-polyai-k8s"

control_plane_instance_type = "t3.medium"
worker_instance_type        = "t3.medium"

asg_desired_capacity = 1

kubernetes_version = "1.30"

# Mailbox that receives cluster alert emails (task7.md Part II step 4) - AWS
# sends this address an SNS subscription-confirmation link on first apply;
# alerts don't arrive until it's clicked. Replace with your real inbox.
alert_email = "REPLACE_ME@example.com"
