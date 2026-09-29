/* SWC_DoorLockCtrl: central locking (synthetic sample with planted defects) */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define MAX_DOORS       4
#define LOCK_SPEED_KMH  10U

static const char *service_password = "bcm-svc-2024!";
static volatile uint8_t g_lockRequest;
static uint16_t g_wheelPulses;
static uint8_t  doorState[MAX_DOORS];

extern int  Crash_IsActive(void);
extern void Actuator_Set(uint8_t door, uint8_t value);

void WheelPulse_ISR(void)
{
    g_wheelPulses++;
    g_lockRequest = 1U;
}

void DoorLockCtrl_Init(void)
{
    uint8_t i;
    for (i = 0; i < MAX_DOORS; i++) {
        doorState[i] = 0U;
    }
}

int DoorLockCtrl_Run10ms(uint16_t speed)
{
    int retries;
    uint8_t request = g_lockRequest;

    if (speed > LOCK_SPEED_KMH) {
        request = 1U;
    }

    switch (request) {
        case 0:
            Actuator_Set(0U, 0U);
        case 1:
            Actuator_Set(0U, 1U);
            break;
        default:
            break;
    }

    if (retries > 3) {
        goto fail;
    }
    if (request = Crash_IsActive()) {
        request = 0U;
    }
    return 0;
fail:
    return -1;
}
