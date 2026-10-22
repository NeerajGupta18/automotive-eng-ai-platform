/*
 * AEB controller implementation. See aeb_ctrl.h and app/modules/adas/model.py (reference model).
 *
 *   1. object confirmation: reported and closing for confirm_cycles (150 ms) before any reaction
 *   2. TTC = distance / closing speed; a_req = deceleration needed to end up stop_gap behind the object,
 *      taking the object's own deceleration (estimated over 200 ms) into account
 *   3. staged response: warning -> partial braking -> full braking; braking is not stepped down while
 *      closing in, released when the object is lost or moving away (above v_min), held at standstill
 *   4. brake request = max(AEB request, driver request)
 */
#include "aeb_ctrl.h"

#include <stddef.h>

#define AEB_CYCLE_S     0.01f
#define AEB_CONFIRM_MAX 65535u

static float aeb_maxf(float a, float b)
{
    return (a > b) ? a : b;
}

void aeb_default_cal(aeb_cal_t *cal)
{
    if (cal != NULL) {
        cal->v_min_mps = 2.77f;
        cal->v_max_mps = 23.62f;
        cal->confirm_cycles = 15u;
        cal->closing_min_mps = 0.5f;
        cal->release_closing_mps = -0.2f;
        cal->ttc_warn_s = 2.6f;
        cal->ttc_partial_s = 1.6f;
        cal->ttc_full_s = 1.0f;
        cal->areq_warn_mps2 = 3.5f;
        cal->areq_full_mps2 = 6.0f;
        cal->a_tgt_min_mps2 = 1.0f;
        cal->a_partial_mps2 = 4.0f;
        cal->a_full_mps2 = 9.0f;
        cal->a_hold_mps2 = 3.0f;
        cal->stop_gap_m = 1.5f;
        cal->standstill_mps = 0.1f;
        cal->ttc_max_s = 99.0f;
    }
}

void aeb_init(aeb_ctx_t *ctx, const aeb_cal_t *cal)
{
    uint8_t i;

    if (ctx != NULL) {
        if (cal != NULL) {
            ctx->cal = *cal;
        } else {
            aeb_default_cal(&ctx->cal);
        }
        ctx->confirm = 0u;
        ctx->state = AEB_STATE_OFF;
        ctx->hist_i = 0u;
        ctx->hist_n = 0u;
        for (i = 0u; i < AEB_HIST_LEN; i++) {
            ctx->hist[i] = 0.0f;
        }
    }
}

static float aeb_ttc(const aeb_cal_t *c, float distance, float closing)
{
    return (closing > 0.1f) ? (distance / closing) : c->ttc_max_s;
}

static float aeb_required_decel(const aeb_cal_t *c, float distance, float closing, float v_ego, float a_tgt)
{
    float d_eff = aeb_maxf(distance - c->stop_gap_m, 0.1f);
    float v_tgt;
    float result;

    if (a_tgt < c->a_tgt_min_mps2) {
        result = (closing > 0.0f) ? ((closing * closing) / (2.0f * d_eff)) : 0.0f;
    } else {
        v_tgt = aeb_maxf(v_ego - closing, 0.0f);
        if ((closing > 0.0f) && ((2.0f * d_eff * a_tgt) < (v_tgt * closing))) {
            /* speeds match before the object stops: relative motion with both braking */
            result = a_tgt + ((closing * closing) / (2.0f * d_eff));
        } else {
            /* the object stops first: stop behind its stopping point */
            result = (v_ego * v_ego) / (2.0f * (d_eff + ((v_tgt * v_tgt) / (2.0f * a_tgt))));
        }
    }
    return result;
}

static float aeb_target_decel(aeb_ctx_t *ctx, const aeb_in_t *in)
{
    float result = 0.0f;
    float v_tgt;
    float old;

    if (in->obj_valid == 0u) {
        ctx->hist_n = 0u;
    } else {
        v_tgt = in->ego_speed_mps - in->closing_mps;
        old = ctx->hist[ctx->hist_i];
        ctx->hist[ctx->hist_i] = v_tgt;
        ctx->hist_i = (uint8_t)((ctx->hist_i + 1u) % AEB_HIST_LEN);
        if (ctx->hist_n < AEB_HIST_LEN) {
            ctx->hist_n++;
        } else {
            result = aeb_maxf((old - v_tgt) / ((float)AEB_HIST_LEN * AEB_CYCLE_S), 0.0f);
        }
    }
    return result;
}

static uint8_t aeb_continue_braking(const aeb_ctx_t *ctx, const aeb_in_t *in, uint8_t confirmed, float ttc, float areq)
{
    const aeb_cal_t *c = &ctx->cal;
    uint8_t next = ctx->state;

    if (in->ego_speed_mps < c->standstill_mps) {
        next = AEB_STATE_HOLD;
    } else if ((in->obj_valid == 0u) ||
               ((in->closing_mps < c->release_closing_mps) && (in->ego_speed_mps >= c->v_min_mps))) {
        next = AEB_STATE_OFF;
    } else if ((confirmed != 0u) && ((ttc < c->ttc_full_s) || (areq > c->areq_full_mps2))) {
        next = AEB_STATE_FULL;
    } else {
        /* keep the current braking level */
    }
    return next;
}

static uint8_t aeb_new_threat(const aeb_cal_t *c, float v, float ttc, float areq)
{
    uint8_t next = AEB_STATE_OFF;

    if ((v <= c->v_max_mps) && ((ttc < c->ttc_full_s) || (areq > c->areq_full_mps2))) {
        next = AEB_STATE_FULL;
    } else if ((v <= c->v_max_mps) && (ttc < c->ttc_partial_s)) {
        next = AEB_STATE_PARTIAL;
    } else if ((ttc < c->ttc_warn_s) || (areq > c->areq_warn_mps2)) {
        next = AEB_STATE_WARN;
    } else {
        /* no threat */
    }
    return next;
}

static uint8_t aeb_decide(const aeb_ctx_t *ctx, const aeb_in_t *in, uint8_t confirmed, float ttc, float areq)
{
    uint8_t next = AEB_STATE_OFF;
    uint8_t s = ctx->state;

    if (s == AEB_STATE_HOLD) {
        next = (in->obj_valid != 0u) ? AEB_STATE_HOLD : AEB_STATE_OFF;
    } else if ((s == AEB_STATE_PARTIAL) || (s == AEB_STATE_FULL)) {
        next = aeb_continue_braking(ctx, in, confirmed, ttc, areq);
    } else if ((confirmed != 0u) && (in->ego_speed_mps >= ctx->cal.v_min_mps)) {
        next = aeb_new_threat(&ctx->cal, in->ego_speed_mps, ttc, areq);
    } else {
        /* not confirmed or below the activation speed */
    }
    return next;
}

static float aeb_request(const aeb_cal_t *c, uint8_t state)
{
    float a = 0.0f;

    switch (state) {
    case AEB_STATE_PARTIAL:
        a = c->a_partial_mps2;
        break;
    case AEB_STATE_FULL:
        a = c->a_full_mps2;
        break;
    case AEB_STATE_HOLD:
        a = c->a_hold_mps2;
        break;
    default:
        a = 0.0f;
        break;
    }
    return a;
}

void aeb_step(aeb_ctx_t *ctx, const aeb_in_t *in, aeb_out_t *out)
{
    const aeb_cal_t *c;
    float a_tgt;
    float ttc;
    float areq = 0.0f;
    float aeb;
    uint8_t confirmed;

    if ((ctx != NULL) && (in != NULL) && (out != NULL)) {
        c = &ctx->cal;
        if ((in->obj_valid != 0u) && (in->closing_mps > c->closing_min_mps)) {
            if (ctx->confirm < AEB_CONFIRM_MAX) {
                ctx->confirm++;
            }
        } else {
            ctx->confirm = 0u;
        }
        a_tgt = aeb_target_decel(ctx, in);
        confirmed = (ctx->confirm >= c->confirm_cycles) ? 1u : 0u;
        ttc = c->ttc_max_s;
        if (confirmed != 0u) {
            ttc = aeb_ttc(c, in->distance_m, in->closing_mps);
            areq = aeb_required_decel(c, in->distance_m, in->closing_mps, in->ego_speed_mps, a_tgt);
        }
        ctx->state = aeb_decide(ctx, in, confirmed, ttc, areq);
        aeb = aeb_request(c, ctx->state);
        out->aeb_req_mps2 = aeb;
        out->brake_req_mps2 = aeb_maxf(aeb, in->driver_brake_mps2);
        out->ttc_s = ttc;
        out->a_req_mps2 = areq;
        out->a_tgt_mps2 = a_tgt;
        out->fcw = ((ctx->state == AEB_STATE_WARN) || (ctx->state == AEB_STATE_PARTIAL) ||
                    (ctx->state == AEB_STATE_FULL)) ? 1u : 0u;
        out->state = ctx->state;
    }
}

uint32_t aeb_ctx_size(void)
{
    return (uint32_t)sizeof(aeb_ctx_t);
}

const char *aeb_version(void)
{
    return AEB_SW_VERSION;
}
