region = "us-east-1"

# Must already exist in AWS (Terraform doesn't manage the key pair, so no
# private key material ever touches state) - see the manual steps for how
# to create it.
key_name = "sawalha-polyai-k8s"

control_plane_instance_type = "t3.medium"
worker_instance_type        = "t3.medium"

asg_desired_capacity = 1

kubernetes_version = "1.30"
