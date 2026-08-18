variable "name_prefix" {
  description = "Prefix used to name/tag every resource this module creates"
  type        = string
}

variable "worker_role_name" {
  description = "Name of the worker nodes' IAM role - Cluster Autoscaler (running as a pod on a worker) calls the Auto Scaling API through this role's instance profile, same no-IRSA pattern as Alertmanager's SNS publish (see infra/k8s/README.md Step 3)"
  type        = string
}

variable "worker_asg_arn" {
  description = "ARN of the worker Auto Scaling Group - scopes the write actions (SetDesiredCapacity, TerminateInstanceInAutoScalingGroup) to just this ASG"
  type        = string
}
