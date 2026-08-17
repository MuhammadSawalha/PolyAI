data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# --- Networking ---

resource "aws_security_group" "nodes" {
  name_prefix = "${var.name_prefix}-nodes-"
  description = "Control plane + worker nodes: SSH and all intra-VPC traffic"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "All intra-VPC traffic (kubelet/apiserver/Calico/node-to-node)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.name_prefix}-nodes"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# --- IAM: control plane ---

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "control_plane" {
  name               = "${var.name_prefix}-control-plane"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "control_plane_eks_cluster" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "control_plane_ebs_csi" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role_policy_attachment" "control_plane_ecr_ro" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Scoped narrowly to the single join-command parameter this module publishes.
resource "aws_iam_role_policy" "control_plane_ssm_publish" {
  name = "${var.name_prefix}-ssm-join-command-publish"
  role = aws_iam_role.control_plane.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:PutParameter", "ssm:GetParameter"]
      Resource = "arn:aws:ssm:*:*:parameter/${var.name_prefix}/*"
    }]
  })
}

resource "aws_iam_instance_profile" "control_plane" {
  name = "${var.name_prefix}-control-plane"
  role = aws_iam_role.control_plane.name
}

# --- IAM: workers ---
# EBS CSI driver + the same S3/Bedrock access already granted to the existing
# dev/prod Compose EC2 hosts (see infra/k8s/README.md Step 3) - these two
# customer-managed policies must already exist in the account.

data "aws_iam_policy" "s3_image_policy" {
  name = "s3-image-policy"
}

data "aws_iam_policy" "bedrock_policy" {
  name = "sawalha-bedrock-policy"
}

resource "aws_iam_role" "worker" {
  name               = "${var.name_prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "worker_ebs_csi" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role_policy_attachment" "worker_s3_images" {
  role       = aws_iam_role.worker.name
  policy_arn = data.aws_iam_policy.s3_image_policy.arn
}

resource "aws_iam_role_policy_attachment" "worker_bedrock" {
  role       = aws_iam_role.worker.name
  policy_arn = data.aws_iam_policy.bedrock_policy.arn
}

resource "aws_iam_role_policy" "worker_ssm_read" {
  name = "${var.name_prefix}-ssm-join-command-read"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:*:*:parameter/${var.name_prefix}/*"
    }]
  })
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.name_prefix}-worker"
  role = aws_iam_role.worker.name
}

# --- Worker join-command handoff ---
# A Terraform-managed placeholder so `destroy` actually removes it - a raw
# `aws ssm put-parameter` from inside user-data alone would leave this
# parameter behind across destroy/recreate cycles, letting a fast-booting
# worker fetch a stale join command still pointing at a control plane from a
# previous, already-destroyed generation (its IP long gone, so the join can
# never succeed). Terraform only owns the parameter's existence here, not its
# value: the control plane overwrites it with the real join command at boot
# (control-plane-init.sh.tpl), and workers reject the "PENDING" placeholder
# until that overwrite has actually happened (worker-init.sh.tpl).
resource "aws_ssm_parameter" "join_command" {
  name  = "/${var.name_prefix}/join-command"
  type  = "SecureString"
  value = "PENDING"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.name_prefix}-join-command"
  }
}

# --- Control plane ---

resource "aws_instance" "control_plane" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.control_plane_instance_type
  subnet_id                   = var.public_subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.nodes.id]
  iam_instance_profile        = aws_iam_instance_profile.control_plane.name
  key_name                    = var.key_name
  associate_public_ip_address = true

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  user_data = templatefile("${path.module}/templates/control-plane-init.sh.tpl", {
    kubernetes_version = var.kubernetes_version
    region             = var.region
    ssm_param_name     = aws_ssm_parameter.join_command.name
  })

  tags = {
    Name = "${var.name_prefix}-control-plane"
    Role = "control-plane"
  }
}

# --- Workers (Auto Scaling Group) ---

resource "aws_launch_template" "worker" {
  name_prefix   = "${var.name_prefix}-worker-"
  image_id      = data.aws_ami.ubuntu.id
  instance_type = var.worker_instance_type
  key_name      = var.key_name

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  vpc_security_group_ids = [aws_security_group.nodes.id]

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size = 20
      volume_type = "gp3"
    }
  }

  user_data = base64encode(templatefile("${path.module}/templates/worker-init.sh.tpl", {
    kubernetes_version = var.kubernetes_version
    region             = var.region
    ssm_param_name     = aws_ssm_parameter.join_command.name
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${var.name_prefix}-worker"
      Role = "worker"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "workers" {
  name                = "${var.name_prefix}-workers"
  min_size            = var.asg_min_size
  max_size            = var.asg_max_size
  desired_capacity    = var.asg_desired_capacity
  vpc_zone_identifier = var.public_subnet_ids

  launch_template {
    id      = aws_launch_template.worker.id
    version = "$Latest"
  }

  # NOTE: on scale-down, the terminated instance's Node object is left behind
  # in the cluster with status NotReady - this isn't cleaned up automatically
  # (see infra/k8s/README.md for the manual `kubectl delete node <name>` step).
  # Automating that removal needs an ASG termination lifecycle hook wired to
  # something that can reach the cluster API (Lambda, SSM Run Command, etc.);
  # left out here to keep this module's moving parts to what's needed to join
  # nodes, not also remove them.

  tag {
    key                 = "Name"
    value               = "${var.name_prefix}-worker"
    propagate_at_launch = true
  }

  # Cluster Autoscaler's AWS auto-discovery (task7.md Part III bonus) finds
  # this ASG by these two tags instead of a hardcoded --nodes flag - see
  # infra/k8s/autoscaler/values.yaml's autoDiscovery.clusterName.
  tag {
    key                 = "k8s.io/cluster-autoscaler/enabled"
    value               = "true"
    propagate_at_launch = true
  }

  tag {
    key                 = "k8s.io/cluster-autoscaler/${var.name_prefix}"
    value               = "owned"
    propagate_at_launch = true
  }

  lifecycle {
    create_before_destroy = true
  }
}
