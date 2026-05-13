Feature: Removing the OTLP relation blocks the unit
  As an operator
  I want telemetrygen-k8s to stop generating telemetry and surface a clear status
  So that it does not silently keep producing data after I drop its destination

  Scenario: Dropping the only send-otlp relation returns the unit to Blocked
    Given the telemetrygen-k8s charm is deployed and related to the OTLP receiver
    And telemetrygen-k8s is Active
    When I remove the relation between telemetrygen-k8s and the OTLP receiver
    Then telemetrygen-k8s reaches Blocked status with a message mentioning "missing required relation"
    When I re-add the relation between telemetrygen-k8s and the OTLP receiver
    Then telemetrygen-k8s returns to Active status
