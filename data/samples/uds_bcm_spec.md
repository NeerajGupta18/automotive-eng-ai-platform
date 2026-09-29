# BCM Diagnostic Specification (synthetic)

ECU: BCM
Version: 1.0

## 1 Diagnostic Sessions

| Session | ID |
|---|---|
| default | 0x01 |
| programming | 0x02 |
| extended | 0x03 |

## 2 Timing

P2 server max: 50 ms
P2* server max: 5000 ms
S3 server timeout: 5000 ms

## 3 Security Access

| Level | Seed length | Max attempts | Sessions |
|---|---|---|---|
| 1 | 2 | 3 | extended |

## 4 Supported Services

| SID | Service | Sessions | Security |
|---|---|---|---|
| 0x10 | DiagnosticSessionControl | default, programming, extended | - |
| 0x11 | ECUReset | default, extended | - |
| 0x14 | ClearDiagnosticInformation | extended | - |
| 0x19 | ReadDTCInformation | default, extended | - |
| 0x22 | ReadDataByIdentifier | default, extended | - |
| 0x27 | SecurityAccess | extended | - |
| 0x2E | WriteDataByIdentifier | extended | - |
| 0x31 | RoutineControl | extended | - |
| 0x3E | TesterPresent | default, programming, extended | - |
| 0x85 | ControlDTCSetting | extended | - |

## 5 Data Identifiers

| DID | Name | Length | Access | Sessions | Security | Range |
|---|---|---|---|---|---|---|
| 0xF190 | VIN | 17 | R | default, extended | - | - |
| 0xF187 | PartNumber | 10 | R | default, extended | - | - |
| 0x0100 | AutoLockSpeedThreshold | 1 | RW | extended | Level 1 | 5-30 |
| 0x0101 | AmbientLightThreshold | 2 | RW | extended | Level 1 | 0-1000 |
| 0x0200 | LockCycleCounter | 2 | R | default, extended | - | - |

## 6 Routines

| RID | Name | Controls | Sessions | Security | Option length |
|---|---|---|---|---|---|
| 0x0203 | DoorActuatorTest | start, stop, results | extended | Level 1 | 1 |
| 0x0301 | ResetLockCounter | start | extended | Level 1 | 0 |

## 7 Diagnostic Trouble Codes

| DTC | Name |
|---|---|
| 0xB10100 | DoorSensorPlausibility |
| 0xC12200 | WheelSpeedSignalLoss |
