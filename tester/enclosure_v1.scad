// ============================================================
//  Plug Tester Enclosure v1
//  Circuit  : layout_tester_v3  (4 LEDs — Verde/Vermelho/Azul/Laranja)
//  Standard : NBR 14136 10A (Brazilian 3-pin round-pin plug)
//
//  STRATEGY: reuse the 3 brass pins from your existing white plug.
//  Unscrew them, press-fit into this new larger body with CA glue.
//
//  PRINT:
//    Set PART = "body"   → export body.stl  (flat bottom on build plate)
//    Set PART = "lid"    → export lid.stl   (flat outer face on build plate)
//    Set PART = "both"   → assembled preview
//
//  HARDWARE:
//    4 × M3 × 10 self-tapping screws  (or M3 bolts + M3 hex nuts)
//    Brass pins from existing NBR 14136 plug  (press-fit + CA glue)
//    Perfboard ~50 × 26 mm  (fits on internal standoffs)
// ============================================================

PART = "both";   // "body" | "lid" | "both"

// ── NBR 14136 10A — pin positions (XY, origin = centroid) ───
//   P1 (-9.5,  4.5)   P2 (+9.5, +4.5)    ← two upper pins
//   T  (  0,  -8.5)                        ← earth pin
//   Distance P1→T along Y = 4.5+8.5 = 13 mm  ✓ (NBR 14136 spec)

P1 = [-9.5,  4.5];   // Pino 1 — phase or L1
P2 = [ 9.5,  4.5];   // Pino 2 — neutral or L2
T  = [  0,  -8.5];   // Terra   — earth

// ── Pin holes ────────────────────────────────────────────────
pin_d      = 4.15;   // shaft hole  (4.0 mm pin + 0.15 tolerance)
pin_base_d = 7.0;    // counterbore ∅ (fits brass shoulder + solder access)
pin_base_h = 4.0;    // counterbore depth from inside floor

// ── Box body ─────────────────────────────────────────────────
wall  = 2.2;     // shell wall
floor = 3.5;     // bottom plate (pin holes pass through here)
box_w = 56.0;    // external width  (X) — wider than 19 mm pin span
box_l = 32.0;    // external length (Y) — fits pin triangle + margin
box_h = 64.0;    // external height (Z) — room for perfboard + components
rc    = 4.0;     // XY corner radius

iw = box_w - 2*wall;   // inner width  = 51.6 mm
il = box_l - 2*wall;   // inner length = 27.6 mm

// ── Lid ──────────────────────────────────────────────────────
lid_t   = 3.0;    // outer plate thickness
lip_h   = 3.5;    // inner lip height (locates + seals onto body)
lip_clr = 0.25;   // radial clearance lip↔inner wall

// ── 4 LED holes on lid (row, 11 mm spacing) ──────────────────
//  Order left→right: 🟢 Verde  🔴 Vermelho  🔵 Azul  🟠 Laranja
led_d   = 5.3;
led_xs  = [-16.5, -5.5, 5.5, 16.5];
led_y   =  5.0;   // offset from lid centerline toward +Y

// ── 2 button holes on lid (BTN1 = Pino1, BTN2 = Pino2) ───────
btn_d  = 7.2;    // 6×6 mm tactile button + 1.2 mm clearance
btn_xs = [-8.0, 8.0];
btn_y  = -7.0;   // below LED row

// ── M3 screw posts (4 corners inside body) ───────────────────
post_od  = 6.5;
post_id  = 2.8;    // 2.8 mm = M3 self-tap in PLA  (use 3.2 for bolt+nut)
post_rim = 0.8;    // gap between post surface and inner wall
post_h   = box_h - floor - 2.0;   // nearly full inner height

px = iw/2 - post_od/2 - post_rim;   // = 21.75
py = il/2 - post_od/2 - post_rim;   // =  9.55
post_pos = [[-px,-py], [-px,py], [px,-py], [px,py]];

// ── Board standoffs (2 central, for 50×26 mm perfboard) ──────
so_od  = 4.5;
so_id  = 1.6;    // M2 self-tap or press-fit pin
so_h   = 14.0;   // board sits 14 mm above floor
so_pos = [[-18, 0], [18, 0]];

// ────────────────────────────────────────────────────────────
$fn = 64;

module rounded_box(w, l, h, r) {
    r = max(0.5, r);
    hull()
        for (xi = [-1,1], yi = [-1,1])
            translate([xi*(w/2 - r), yi*(l/2 - r), 0])
                cylinder(r=r, h=h);
}

// ────────────────────────────────────────────────────────────
module body() {
    difference() {

        // ── Outer shell ──
        rounded_box(box_w, box_l, box_h, rc);

        // ── Inner cavity (open at top) ──
        translate([0, 0, floor])
            rounded_box(iw, il, box_h, rc - wall);

        // ── Pin shaft holes (through floor) ──
        for (p = [P1, P2, T]) {
            // Through-hole for pin shaft
            translate([p[0], p[1], -0.1])
                cylinder(d=pin_d, h=floor + 0.2);
            // Counterbore from inside (retains brass shoulder, allows solder access)
            translate([p[0], p[1], floor - pin_base_h])
                cylinder(d=pin_base_d, h=pin_base_h + 0.1);
        }

        // ── Vent slot on side (wire routing / airflow) ──
        translate([0, box_l/2, floor + 4])
            cube([20, wall*2+0.2, 6], center=true);

    } // end difference

    // ── Corner screw posts ──
    for (pp = post_pos)
        translate([pp[0], pp[1], floor])
            difference() {
                cylinder(d=post_od, h=post_h);
                cylinder(d=post_id, h=post_h);      // blind screw hole
            }

    // ── Board standoffs ──
    for (sp = so_pos)
        translate([sp[0], sp[1], floor])
            difference() {
                cylinder(d=so_od, h=so_h);
                cylinder(d=so_id, h=so_h);
            }
}

// ────────────────────────────────────────────────────────────
//  Lid: printed flat outer face DOWN, lip pointing UP.
//  When assembled: flip upside-down so lip goes into body opening.
module lid() {
    iw_lip = iw - 2*lip_clr;
    il_lip = il - 2*lip_clr;
    rc_lip = max(0.5, rc - wall - lip_clr);

    difference() {
        union() {
            // Inner lip (goes into body opening when assembled)
            rounded_box(iw_lip, il_lip, lip_h, rc_lip);
            // Outer plate (flat external face)
            translate([0, 0, lip_h])
                rounded_box(box_w, box_l, lid_t, rc);
        }

        // ── LED holes (all the way through lip + plate) ──
        for (lx = led_xs)
            translate([lx, led_y, -0.1])
                cylinder(d=led_d, h=lip_h + lid_t + 0.2);

        // ── Button holes ──
        for (bx = btn_xs)
            translate([bx, btn_y, -0.1])
                cylinder(d=btn_d, h=lip_h + lid_t + 0.2);

        // ── Screw clearance holes ──
        for (pp = post_pos)
            translate([pp[0], pp[1], -0.1])
                cylinder(d=post_id + 0.8, h=lip_h + lid_t + 0.2);
    }
}

// ────────────────────────────────────────────────────────────
// Render

if (PART == "body") {
    body();

} else if (PART == "lid") {
    // Print orientation: outer face down (rotate so lip points up)
    translate([0, 0, lid_t + lip_h])
        rotate([180, 0, 0])
            lid();

} else {
    // Assembled preview
    body();
    // Lid sits on top: lip goes into body (below box_h), plate above
    translate([0, 0, box_h - lip_h])
        lid();
}

// ────────────────────────────────────────────────────────────
// NOTES:
//
//  Pin installation:
//    1. Remove brass pins from your white NBR 14136 plug
//       (unscrew the retaining screws from inside the original plug)
//    2. Push pins through the floor holes from outside (pin tip first)
//    3. The shoulder sits in the counterbore inside — add CA glue
//    4. Inside the counterbore, solder wires to the pin bases
//
//  LED installation:
//    LEDs go through holes in the lid from inside.
//    Glue in place or rely on friction fit (5.3 mm hole, 5 mm LED).
//
//  Lid orientation reminder:
//    The hole labels on the lid (when looking at outer face):
//      LEFT row, L→R:  🟢 Verde  🔴 Vermelho  🔵 Azul  🟠 Laranja
//      Below:          BTN1 (Pino1 continuity)  BTN2 (Pino2 continuity)
//
//  If adding buzzer (layout_tester_buzzer):
//    The buzzer fits flat on the perfboard.
//    No extra holes needed in the enclosure.
// ============================================================
