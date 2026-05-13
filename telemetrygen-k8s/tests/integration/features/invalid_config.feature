Feature: Invalid configuration blocks the unit
  As an operator
  I want the charm to refuse to run with a malformed config option
  So that I learn about the mistake immediately, not by silent data loss downstream

  Scenario: Setting duration to a non-Go-duration string blocks the unit
    Given the telemetrygen-k8s charm is deployed and related to the OTLP receiver
    And telemetrygen-k8s is Active
    When I set the telemetrygen-k8s "duration" config to "abc"
    Then telemetrygen-k8s reaches Blocked status with a message mentioning "duration"
    When I reset the telemetrygen-k8s "duration" config back to "inf"
    Then telemetrygen-k8s returns to Active status
