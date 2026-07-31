variable "name_prefix" {
  description = "Prefix used to name/tag every resource this module creates"
  type        = string
}

variable "region" {
  description = "AWS region (passed through to user-data scripts for aws cli/ssm calls)"
  type        = string
}

variable "vpc_id" {
  description = "VPC to provision the cluster nodes into"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC, used to allow all intra-VPC traffic on the node security group"
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs the control plane and worker ASG launch into"
  type        = list(string)
}

variable "key_name" {
  description = "Name of an existing EC2 key pair used for SSH access"
  type        = string
}

variable "control_plane_instance_type" {
  type = string
}

variable "worker_instance_type" {
  type = string
}

variable "asg_min_size" {
  type = number
}

variable "asg_max_size" {
  type = number
}

variable "asg_desired_capacity" {
  type = number
}

variable "kubernetes_version" {
  type = string
}
