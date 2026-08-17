output "sns_topic_arn" {
  description = "ARN of the SNS topic Alertmanager publishes alerts to - passed into the kube-prometheus-stack Helm install via --set (infra/k8s/bootstrap.sh), not committed to infra/k8s/monitoring/values.yaml since it's only known after apply"
  value       = aws_sns_topic.alerts.arn
}
