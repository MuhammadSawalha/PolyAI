# IAM permissions for the AWS Cluster Autoscaler (task7.md Part III bonus).
# Runs as a pod on a worker node and authenticates through the worker's own
# EC2 instance profile - no IRSA, since this is a self-managed kubeadm
# cluster, not EKS (same pattern as Alertmanager's SNS publish in
# infra/tf/modules/monitoring).
#
# The autoscaler's read calls (Describe*) don't support resource-level
# restriction in IAM - AWS requires Resource = "*" for them. The two calls
# that actually change cluster capacity (SetDesiredCapacity,
# TerminateInstanceInAutoScalingGroup) are scoped to this cluster's one
# worker ASG so this policy can't touch any other ASG in the account.
resource "aws_iam_role_policy" "worker_cluster_autoscaler" {
  name = "${var.name_prefix}-cluster-autoscaler"
  role = var.worker_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Read"
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations",
          "autoscaling:DescribeScalingActivities",
          "autoscaling:DescribeTags",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplateVersions",
        ]
        Resource = "*"
      },
      {
        Sid    = "Write"
        Effect = "Allow"
        Action = [
          "autoscaling:SetDesiredCapacity",
          "autoscaling:TerminateInstanceInAutoScalingGroup",
        ]
        Resource = var.worker_asg_arn
      }
    ]
  })
}
