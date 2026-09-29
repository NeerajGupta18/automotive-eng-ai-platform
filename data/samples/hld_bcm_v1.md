# Body Control Module (BCM) - High-Level Design

Document ID: HLD-BCM-001
Version: 1.0
Status: Approved for review

## 1 Introduction

This document describes the AUTOSAR high-level software architecture of the Body Control Module (BCM). The BCM manages door locking, exterior lighting and door status monitoring for a passenger vehicle. It runs on a 32-bit microcontroller with AUTOSAR Classic Platform 4.4.

## 2 System Overview

The BCM receives vehicle speed from the powertrain CAN network and door sensor signals from the door modules. Application software components communicate exclusively through the RTE. The BCM implements auto-lock: all doors are locked automatically when the vehicle speed exceeds 10 km/h. The BCM also switches on the headlamps when the ambient light level falls below the configured threshold.

The design follows the layered AUTOSAR approach. Application SWCs do not access hardware directly; they use the ECU abstraction and complex device drivers.

## 3 Software Components

### 3.1 SWC_DoorLockCtrl

Type: Application SWC
Description: Controls central locking. Implements auto-lock above 10 km/h and unlock after a crash event.
Runnables: DoorLockCtrl_Run10ms, DoorLockCtrl_Init

| Port | Direction | Interface |
|---|---|---|
| pDoorLockCmd | P-Port | If_DoorLockCmd |
| rVehicleSpeed | R-Port | If_VehicleSpeed |
| rCrashState | R-Port | If_CrashState |

### 3.2 SWC_VehicleSpeedMgr

Type: Application SWC
Description: Filters and plausibility-checks the wheel pulse signals and provides the vehicle speed in km/h.
Runnables: VehicleSpeedMgr_Run10ms

| Port | Direction | Interface |
|---|---|---|
| pVehicleSpeed | P-Port | If_VehicleSpeed |
| rWheelPulse | R-Port | If_WheelPulse |

### 3.3 SWC_WheelSensorIf

Type: Sensor-Actuator SWC
Description: Abstracts the four wheel speed sensors and provides raw pulse counts.
Runnables: WheelSensorIf_Run5ms

| Port | Direction | Interface |
|---|---|---|
| pWheelPulse | P-Port | If_WheelPulse |

### 3.4 SWC_DoorStateMgr

Type: Application SWC
Description: Aggregates the raw door sensor states into a single door state and reacts to lock commands.
Runnables: DoorStateMgr_Run20ms

| Port | Direction | Interface |
|---|---|---|
| pDoorState | P-Port | If_DoorState |
| rDoorSensorRaw | R-Port | If_DoorSensorRaw |
| rDoorLockCmd | R-Port | If_DoorLockCmd |

### 3.5 SWC_DoorSensorIf

Type: Sensor-Actuator SWC
Description: Reads the door contact sensors through the I/O hardware abstraction.
Runnables: DoorSensorIf_Run20ms

| Port | Direction | Interface |
|---|---|---|
| pDoorSensorRaw | P-Port | If_DoorSensorRaw |

### 3.6 SWC_LightCtrl

Type: Application SWC
Description: Controls the headlamps based on ambient light and door state. Welcome lighting is activated when a door opens.
Runnables: LightCtrl_Run50ms

| Port | Direction | Interface |
|---|---|---|
| rDoorState | R-Port | If_DoorState |
| rIgnitionState | R-Port | If_IgnState |
| pHeadlampCmd | P-Port | If_HeadlampCmd |
| rHeadlampFeedback | P-Port | If_HeadlampFeedback |

## 4 Port Interfaces

### 4.1 If_VehicleSpeed

Type: SenderReceiver
Data elements: VehicleSpeed (uint16, km/h)

### 4.2 If_WheelPulse

Data elements: PulseCountFL (uint16); PulseCountFR (uint16); PulseCountRL (uint16); PulseCountRR (uint16)

### 4.3 If_DoorLockCmd

Type: SenderReceiver
Data elements: LockRequest (uint8)

### 4.4 If_DoorState

Type: SenderReceiver
Data elements: DoorStateFL (uint8); DoorStateFR (uint8); DoorStateRL (uint8); DoorStateRR (uint8)

### 4.5 If_DoorSensorRaw

Type: SenderReceiver
Data elements: RawContactBits (uint8)

### 4.6 If_CrashState

Type: SenderReceiver
Data elements: CrashDetected (boolean)

### 4.7 If_HeadlampCmd

Type: SenderReceiver
Data elements: HeadlampRequest (uint8)

### 4.8 If_HeadlampFeedback

Type: SenderReceiver
Data elements: LampFault (boolean)

### 4.9 If_UnusedDiag

Type: ClientServer
Operations: ReadDiagCounter; ResetDiagCounter

## 5 Timing and Scheduling

Runnables are mapped to periodic OS tasks of 5 ms, 10 ms, 20 ms and 50 ms. The auto-lock decision in DoorLockCtrl_Run10ms shall be made within 20 ms of a vehicle speed update. Data consistency between tasks is ensured by the RTE using implicit communication.

## 6 Diagnostics

Faults detected by the SWCs are reported to the Diagnostic Event Manager (Dem). Door sensor plausibility failures are stored as DTC 0xB10100. The wheel speed signal loss is stored as DTC 0xC12200.
