output "control_plane_public_ip" {
  description = "Public IP of the control-plane EC2 instance"
  value       = module.k8s_cluster.control_plane_public_ip
}

output "vpc_id" {
  description = "ID of the cluster VPC"
  value       = module.vpc.vpc_id
}

output "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group"
  value       = module.k8s_cluster.worker_asg_name
}
