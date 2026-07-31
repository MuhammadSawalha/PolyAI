variable "region" {
  description = "AWS region to provision the cluster in"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the cluster VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "key_name" {
  description = "Name of an existing EC2 key pair used for SSH access to the control plane and worker nodes"
  type        = string
}

variable "control_plane_instance_type" {
  description = "Instance type for the control-plane EC2 instance"
  type        = string
  default     = "t3.medium"
}

variable "worker_instance_type" {
  description = "Instance type used by the worker Auto Scaling Group's launch template"
  type        = string
  default     = "t3.medium"
}

variable "asg_min_size" {
  description = "Minimum number of worker nodes"
  type        = number
  default     = 1
}

variable "asg_max_size" {
  description = "Maximum number of worker nodes"
  type        = number
  default     = 3
}

variable "asg_desired_capacity" {
  description = "Desired number of worker nodes. Set to 0 to scale the worker fleet down when the cluster isn't in use"
  type        = number
  default     = 1
}

variable "kubernetes_version" {
  description = "Kubernetes minor version to install (e.g. 1.30)"
  type        = string
  default     = "1.30"
}
