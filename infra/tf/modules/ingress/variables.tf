variable "name_prefix" {
  description = "Prefix used to name/tag every resource this module creates"
  type        = string
}

variable "vpc_id" {
  description = "VPC the ALB's security group and target group live in"
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs the ALB is placed into"
  type        = list(string)
}

variable "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group to attach to the target group - the ASG then registers/deregisters instances with it automatically as it scales"
  type        = string
}

variable "http_node_port" {
  description = "Fixed NodePort the ingress-nginx controller's Service exposes HTTP (80) on - must match the value infra/k8s/bootstrap.sh patches the Service to"
  type        = number
}

variable "route53_zone_name" {
  description = "Name of the pre-existing, shared hosted zone to add DNS records into (looked up via data source - this module never manages the zone itself, so terraform destroy can't take it down)"
  type        = string
}

variable "domain_name" {
  description = "Base domain under route53_zone_name; a wildcard ACM cert and Route 53 alias record are created for *.<domain_name>, and Ingress manifests use hosts under it (e.g. frontend-dev.<domain_name>)"
  type        = string
}
