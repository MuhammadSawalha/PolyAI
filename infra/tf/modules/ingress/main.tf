# Looked up, never managed - so `terraform destroy` can't delete a hosted
# zone shared with other students/stacks.
data "aws_route53_zone" "shared" {
  name         = "${var.route53_zone_name}."
  private_zone = false
}

# --- ALB ---

resource "aws_security_group" "alb" {
  name_prefix = "${var.name_prefix}-alb-"
  description = "ALB: HTTPS from the internet"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.name_prefix}-alb"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  tags = {
    Name = "${var.name_prefix}-alb"
  }
}

# Targets the ingress-nginx NodePort over plain HTTP - the ALB is the TLS
# termination point (see aws_lb_listener.https), not the nodes.
resource "aws_lb_target_group" "http" {
  name        = "${var.name_prefix}-tg"
  port        = var.http_node_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  health_check {
    protocol = "HTTP"
    path     = "/healthz"
    port     = "traffic-port"
    # No workload actually serves /healthz through the ingress - ingress-nginx's
    # own default backend answers with 404 for any unmatched host/path, and
    # that 404 is exactly what proves the controller pod is alive and kube-proxy
    # is routing the NodePort correctly.
    matcher             = "200-404"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
  }

  tags = {
    Name = "${var.name_prefix}-tg"
  }
}

# The ASG itself keeps this target group's registrations in sync as it scales
# workers up/down - no per-instance aws_lb_target_group_attachment needed.
resource "aws_autoscaling_attachment" "workers" {
  autoscaling_group_name = var.worker_asg_name
  lb_target_group_arn    = aws_lb_target_group.http.arn
}

# --- ACM certificate (DNS-validated via records in the shared zone) ---

resource "aws_acm_certificate" "wildcard" {
  domain_name               = "*.${var.domain_name}"
  subject_alternative_names = [var.domain_name]
  validation_method         = "DNS"

  tags = {
    Name = "${var.name_prefix}-cert"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.wildcard.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }

  zone_id         = data.aws_route53_zone.shared.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "wildcard" {
  certificate_arn         = aws_acm_certificate.wildcard.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# --- HTTPS listener ---

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-2016-08"
  certificate_arn   = aws_acm_certificate_validation.wildcard.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.http.arn
  }
}

# --- DNS ---
# Single wildcard alias record - every Ingress host (frontend-dev, agent-dev,
# grafana-prod, argocd, ...) resolves through this one record straight to the
# ALB; routing to the right Service happens inside ingress-nginx by Host
# header, not in Route 53 or the ALB.
resource "aws_route53_record" "wildcard" {
  zone_id = data.aws_route53_zone.shared.zone_id
  name    = "*.${var.domain_name}"
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}
