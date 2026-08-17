# SNS topic Alertmanager publishes to (via its native sns_config receiver,
# see infra/k8s/monitoring/values.yaml) and fans out to a real mailbox -
# task7.md Part II step 4.
resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Alertmanager runs as a pod on a worker node and authenticates the same way
# every other AWS call from this cluster does - through the worker's own EC2
# instance profile, no IRSA/Secret (see infra/k8s/README.md Step 3). Scoped
# to just this one topic.
resource "aws_iam_role_policy" "worker_sns_publish" {
  name = "${var.name_prefix}-alertmanager-sns-publish"
  role = var.worker_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sns:Publish"]
      Resource = aws_sns_topic.alerts.arn
    }]
  })
}
