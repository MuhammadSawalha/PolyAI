terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.55"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.9"
    }
  }

  # bucket/region are intentionally omitted here (backend blocks can't reference
  # variables) - supply them with `terraform init -backend-config="bucket=..." \
  # -backend-config="region=..."`. All workspaces (one per region) share this
  # bucket; the workspace name keeps their state files apart.
  backend "s3" {
    key = "polyai/k8s-cluster.tfstate"
  }
}

provider "aws" {
  region = var.region
}

data "aws_availability_zones" "available" {
  state = "available"
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.8"

  name = "sawalha-polyai-${terraform.workspace}"
  cidr = var.vpc_cidr

  azs             = slice(data.aws_availability_zones.available.names, 0, 2)
  public_subnets  = [cidrsubnet(var.vpc_cidr, 4, 0), cidrsubnet(var.vpc_cidr, 4, 1)]
  private_subnets = []

  enable_nat_gateway      = false
  map_public_ip_on_launch = true

  tags = {
    Workspace = terraform.workspace
  }
}

module "k8s_cluster" {
  source = "./modules/k8s-cluster"

  name_prefix       = "sawalha-polyai-${terraform.workspace}"
  region            = var.region
  vpc_id            = module.vpc.vpc_id
  vpc_cidr          = module.vpc.vpc_cidr_block
  public_subnet_ids = module.vpc.public_subnets

  key_name = var.key_name

  control_plane_instance_type = var.control_plane_instance_type
  worker_instance_type        = var.worker_instance_type

  asg_min_size         = var.asg_min_size
  asg_max_size         = var.asg_max_size
  asg_desired_capacity = var.asg_desired_capacity

  kubernetes_version = var.kubernetes_version
}

module "ingress" {
  source = "./modules/ingress"

  name_prefix       = "sawalha-polyai-${terraform.workspace}"
  vpc_id            = module.vpc.vpc_id
  public_subnet_ids = module.vpc.public_subnets
  worker_asg_name   = module.k8s_cluster.worker_asg_name

  http_node_port    = var.ingress_http_node_port
  route53_zone_name = var.route53_zone_name
  domain_name       = var.domain_name
}

module "monitoring" {
  source = "./modules/monitoring"

  name_prefix      = "sawalha-polyai-${terraform.workspace}"
  worker_role_name = module.k8s_cluster.worker_role_name
  alert_email      = var.alert_email
}

module "autoscaler" {
  source = "./modules/autoscaler"

  name_prefix      = "sawalha-polyai-${terraform.workspace}"
  worker_role_name = module.k8s_cluster.worker_role_name
  worker_asg_arn   = module.k8s_cluster.worker_asg_arn
}
