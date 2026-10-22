# Powertrain Gateway (PGW) - High-Level Design (synthetic)

Document ID: HLD-PGW-004
Version: 2.0
Status: Approved

## 1 Introduction

The Powertrain Gateway (PGW) connects the chassis CAN, the powertrain CAN and the body CAN networks and the OBD diagnostic connector. It routes signals between the networks, filters diagnostic traffic and coordinates over-the-air (OTA) software updates for the connected ECUs. It runs on AUTOSAR Classic Platform 4.4 with a hardware security module (HSM).

## 2 Network Overview

The chassis CAN and the powertrain CAN run at 500 kbit/s. The body CAN runs at 125 kbit/s. The OBD connector is attached to a dedicated diagnostic CAN that is isolated from the other networks by the gateway firewall.

## 3 Software Components

### 3.1 SWC_SignalRouter

Type: Application SWC
Description: Forwards the vehicle speed received from the ABS ECU on the chassis CAN to the body CAN every 20 ms. If the ABS message is missing for more than 100 ms, the router sends the signal with the status invalid.
Runnables: SignalRouter_Run10ms

| Port | Direction | Interface |
|---|---|---|
| rAbsVehicleSpeed | R-Port | If_AbsVehicleSpeed |
| pBodyVehicleSpeed | P-Port | If_BodyVehicleSpeed |

### 3.2 SWC_GatewayFirewall

Type: Application SWC
Description: Filters the messages arriving from the diagnostic CAN. Only CAN identifiers on the allow-list are forwarded. WriteDataByIdentifier (0x2E) and RoutineControl (0x31) requests are blocked unless the target ECU reports an unlocked security level. Every blocked request is logged as a security event.
Runnables: GatewayFirewall_Run1ms

| Port | Direction | Interface |
|---|---|---|
| rDiagFrame | R-Port | If_DiagFrame |
| pFirewallEvent | P-Port | If_SecurityEvent |

### 3.3 SWC_OtaCoordinator

Type: Application SWC
Description: Coordinates over-the-air updates. It verifies the ECDSA P-256 signature of every update package with a key stored in the HSM, installs the package into the inactive partition (A/B scheme) and switches partitions only after the target ECU confirms a successful boot. If the confirmation does not arrive within 60 seconds, the previous partition is restored (rollback).
Runnables: OtaCoordinator_Run100ms

| Port | Direction | Interface |
|---|---|---|
| rUpdatePackage | R-Port | If_UpdatePackage |
| pUpdateStatus | P-Port | If_UpdateStatus |

### 3.4 SWC_IntrusionDetection

Type: Application SWC
Description: Monitors message cycle times on all networks. A message that arrives more than twice as often as its specified cycle time is reported as a possible injection attack.
Runnables: IntrusionDetection_Run10ms

| Port | Direction | Interface |
|---|---|---|
| rBusStatistics | R-Port | If_BusStatistics |
| pIdsEvent | P-Port | If_SecurityEvent |

## 4 Diagnostics

Loss of communication with the ABS ECU is stored as DTC U0121. An update package with an invalid signature is rejected and stored as DTC U2100.
