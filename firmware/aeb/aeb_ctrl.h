/*
 * AEB (Autonomous Emergency Braking) longitudinal controller, car-to-car rear scenarios.
 *
 * Production-style C99 implementation of the reference model in app/modules/adas/model.py.
 * Called every 10 ms. No dynamic memory, no recursion, no I/O; all state lives in aeb_ctx_t.
 * Back-to-back tests compare this code with the model on identical inputs (software-in-the-loop).
 *
 * Calibration values are example values for a demonstration, not a validated production calibration.
 */
#ifndef AEB_CTRL_H
#define AEB_CTRL_H

#include <stdint.h>

#define AEB_SW_VERSION "1.0.0"
#define AEB_HIST_LEN   20u          /* target speed history, 20 x 10 ms = 200 ms */

#define AEB_STATE_OFF     0u
#define AEB_STATE_WARN    1u
#define AEB_STATE_PARTIAL 2u
#define AEB_STATE_FULL    3u
#define AEB_STATE_HOLD    4u

typedef struct {
    float    v_min_mps;             /* lowest speed for warnings and new braking */
    float    v_max_mps;             /* highest speed for new braking */
    uint16_t confirm_cycles;        /* cycles an object must be reported and closing */
    float    closing_min_mps;       /* closing speed counted as "closing" */
    float    release_closing_mps;   /* braking released below this closing speed (object moving away) */
    float    ttc_warn_s;
    float    ttc_partial_s;
    float    ttc_full_s;
    float    areq_warn_mps2;
    float    areq_full_mps2;
    float    a_tgt_min_mps2;        /* object deceleration below this is treated as noise */
    float    a_partial_mps2;
    float    a_full_mps2;
    float    a_hold_mps2;
    float    stop_gap_m;
    float    standstill_mps;
    float    ttc_max_s;
} aeb_cal_t;

typedef struct {
    float   ego_speed_mps;
    uint8_t obj_valid;              /* 1: an in-path object is reported */
    float   distance_m;             /* bumper to bumper */
    float   closing_mps;            /* ego speed minus object speed, positive when closing in */
    float   driver_brake_mps2;      /* deceleration requested by the driver's brake pedal */
} aeb_in_t;

typedef struct {
    float   brake_req_mps2;         /* sent to the brake system: max(AEB, driver) */
    float   aeb_req_mps2;
    float   ttc_s;
    float   a_req_mps2;
    float   a_tgt_mps2;
    uint8_t fcw;                    /* forward collision warning to the driver */
    uint8_t state;                  /* AEB_STATE_* */
} aeb_out_t;

typedef struct {
    aeb_cal_t cal;
    uint16_t  confirm;
    uint8_t   state;
    uint8_t   hist_i;
    uint8_t   hist_n;
    float     hist[AEB_HIST_LEN];
} aeb_ctx_t;

void        aeb_default_cal(aeb_cal_t *cal);
void        aeb_init(aeb_ctx_t *ctx, const aeb_cal_t *cal);     /* cal == NULL: default calibration */
void        aeb_step(aeb_ctx_t *ctx, const aeb_in_t *in, aeb_out_t *out);
uint32_t    aeb_ctx_size(void);                                  /* lets test harnesses check the struct layout */
const char *aeb_version(void);

#endif /* AEB_CTRL_H */
