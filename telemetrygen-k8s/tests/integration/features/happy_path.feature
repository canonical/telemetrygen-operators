Feature: Generate OTLP telemetry to a real receiver
  As an observability engineer
  I want telemetrygen-k8s to ship synthetic OTLP data to a related receiver
  So that I can exercise an end-to-end COS pipeline

  Scenario: Telemetry actually reaches the receiver after relating
    Given the telemetrygen-k8s charm is deployed and blocked on the missing OTLP relation
    And an opentelemetry-collector-k8s receiver is deployed with debug-export enabled for traces
    When I relate telemetrygen-k8s to the receiver on send-otlp / receive-otlp
    Then telemetrygen-k8s reaches Active status
    And the traces pebble service is running in the workload container
    And the receiver's workload logs contain at least one received trace span
