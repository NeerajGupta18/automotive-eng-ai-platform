# BCM Software Requirements Specification (synthetic)

Document ID: SRS-BCM-002
Version: 1.0
Status: Approved

## 1 Locking Requirements

SRS-BCM-001: The BCM shall lock all doors when the vehicle speed exceeds the auto-lock threshold for longer than 1 second.

SRS-BCM-002: The auto-lock threshold shall be configurable between 5 and 30 km/h through DID 0x0100. The default value is 10 km/h.

SRS-BCM-003: The BCM shall unlock all doors within 100 ms after a crash event is detected. This requirement is allocated ASIL B.

SRS-BCM-004: Lock and unlock commands from the remote key shall be ignored while the vehicle speed is above 3 km/h.

## 2 Lighting Requirements

SRS-BCM-010: The BCM shall switch on the low beam headlamps when the ambient light level stays below the configured threshold for 2 seconds.

SRS-BCM-011: The headlamps shall switch off when the ambient light level stays above the threshold plus a hysteresis of 50 lux for 5 seconds.

SRS-BCM-012: Welcome lighting shall stay on for 30 seconds after a door is opened, or until the ignition is switched on.

## 3 Diagnostic and Monitoring Requirements

SRS-BCM-020: If no vehicle speed message is received for 100 ms, the BCM shall store DTC 0xC12200 and disable auto-lock until a valid message is received.

SRS-BCM-021: A door sensor reading that contradicts the lock actuator feedback for more than 500 ms shall be reported as DTC 0xB10100.

SRS-BCM-022: After three failed security access attempts the BCM shall reject further attempts for 10 seconds.

## 4 Software Update Requirements

SRS-BCM-030: The BCM shall accept software updates only through the OTA coordinator of the Powertrain Gateway.

SRS-BCM-031: The BCM shall report its software version through DID 0xF189 after every update.
