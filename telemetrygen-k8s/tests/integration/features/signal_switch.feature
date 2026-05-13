Feature: Switching signals while related reshapes the running Pebble services
  As an operator
  I want to change which OTLP signals telemetrygen-k8s generates without re-deploying
  So that I can incrementally exercise traces, metrics, and logs from the same unit

  Scenario: Expanding signals from traces to traces,metrics,logs starts more services
    Given the telemetrygen-k8s charm is deployed and related to the OTLP receiver
    And telemetrygen-k8s is Active
    And only the traces pebble service is running
    When I set the telemetrygen-k8s "signals" config to "traces,metrics,logs"
    Then telemetrygen-k8s returns to Active status
    And traces, metrics, and logs pebble services are all running
