output "control_plane_public_ip" {
  description = "Public IP of the control-plane EC2 instance"
  value       = aws_instance.control_plane.public_ip
}

output "control_plane_instance_id" {
  description = "Instance ID of the control-plane EC2 instance"
  value       = aws_instance.control_plane.id
}

output "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group"
  value       = aws_autoscaling_group.workers.name
}

output "worker_asg_arn" {
  description = "ARN of the worker Auto Scaling Group - scopes Cluster Autoscaler's SetDesiredCapacity/TerminateInstanceInAutoScalingGroup IAM permissions to just this ASG (infra/tf/modules/autoscaler)"
  value       = aws_autoscaling_group.workers.arn
}

output "worker_role_name" {
  description = "Name of the worker nodes' IAM role - pods (e.g. Alertmanager) publish to AWS APIs through this role's instance profile, no IRSA"
  value       = aws_iam_role.worker.name
}
