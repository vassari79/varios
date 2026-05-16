# Plug Tester 127V / 220V / Terra
## Complete Project Documentation

Final design for a passive plug tester to detect:
- Outlet voltage (127V or 220V)
- Earth connection (TERRA)
- Phase polarity (which flat pin is FASE)
- Continuity test (neutral-to-earth via buttons)

---

## Final Design Overview

### Three main canals + buzzer circuit:

| Canal | What it measures | Resistors | LED |
|-------|------------------|-----------|-----|
| **A** | Voltage between Pino 1 ↔ Pino 2 | 4×10kΩ = 40kΩ | 🟢 Verde |
| **B** | Voltage between Pino 1 ↔ Pino 2 (high threshold) | 2×47kΩ = 94kΩ | 🔴 Vermelho |
| **C1** | Pino 1 → TERRA | 4×10kΩ = 40kΩ | 🔵 Azul |
| **C2** | Pino 2 → TERRA | 4×10kΩ = 40kΩ | 🟠 Laranja |
| **Buzzer** | Continuity Pino→TERRA (BTN1/BTN2) | 1×1kΩ | 🔊 Piezo |

---

## How it Works

### The optocoupler (PC817C)

The PC817C contains a **LED + transistor** inside, isolated optically:

```
        PC817
    ┌────────────┐
1 ──┤ A    ↗    ├── 4  Collector
    │  LED  light │
2 ──┤ K    ↘    ├── 3  Emitter
    │       TR   │
    └────────────┘
    AC side    DC side
```

- **Pin 1** = Anode (+) of internal LED
- **Pin 2** = Cathode (−) of internal LED
- **Pin 3** = Emitter of transistor
- **Pin 4** = Collector of transistor

The internal LED is illuminated by current from the mains side → activates the transistor → closes the DC circuit → external indicator LED lights.

### Identification: notch + dot mark Pin 1

---

## Component Calculations

### Canal A — fires at BOTH 127V and 220V

Need at least 1mA through the PC817 LED:

```
127V: I = V_peak / (π × R) = 180V / (π × 40,000Ω) = 1.43mA ✅
220V: I = V_peak / (π × R) = 311V / (π × 40,000Ω) = 2.48mA ✅
```

### Canal B — fires ONLY at 220V

Higher resistance threshold to discriminate:

```
127V: I = 180V / (π × 94,000Ω) = 0.61mA ❌ doesn't fire
220V: I = 311V / (π × 94,000Ω) = 1.05mA ✅ fires
```

### Canal C1 / C2 — polarity + earth detection

Each measures one flat pin to TERRA. In Brazil's three-phase system:

| Outlet | Pin to TERRA |
|--------|-------------|
| 127V | only FASE pin = 127V |
| 220V | both pins = 127V (different phases, 120° apart) |

Phase-to-phase voltage = 127V × √3 = 220V (because phases are 120° apart, not 180°)

### LED current limiting (DC side)

```
With CR2032 (3V) and 100Ω:
I = (3V - 2V LED drop) / 100Ω = 10mA ✅ bright
```

### Buzzer continuity test

```
3V battery → 1kΩ → buzzer → button → pin → TERRA
I = 3V / 1kΩ ≈ 3mA (when continuity exists)
```

---

## Reading Guide

### LED combinations:

| 🟢 | 🔴 | 🔵 | 🟠 | Diagnosis |
|----|----|----|----|---------|
| ❌ | ❌ | ❌ | ❌ | No power |
| ✅ | ❌ | ✅ | ❌ | 127V — Pino 1 = FASE — earth ok ✅ |
| ✅ | ❌ | ❌ | ✅ | 127V — Pino 2 = FASE — earth ok ✅ |
| ✅ | ✅ | ✅ | ✅ | 220V — earth ok ✅ |
| ✅ | ❌ | ❌ | ❌ | 127V — **no earth** ⚠️ |
| ✅ | ✅ | ❌ | ❌ | 220V — **no earth** ⚠️ |

### Buzzer:

- Press BTN1 → tests Pino 1 continuity with TERRA
- Press BTN2 → tests Pino 2 continuity with TERRA
- **Buzzer sounds** = continuity confirmed (pin is bonded to earth or is NEUTRO)
- **Silent** = no continuity path

---

## Complete Component List

### Already in your kit ✅

| Qty | Component | Used |
|-----|-----------|------|
| 16 | Resistor 10kΩ 1/4W | 4×A + 4×C1 + 4×C2 |
| 2 | Resistor 47kΩ 1/4W | Canal B |
| 4 | Resistor 100Ω 1/4W | LED limiting |
| 1 | Resistor 1kΩ 1/4W | Buzzer |
| 2 | Push button 4-pin DIP (momentary) | BTN1 + BTN2 |

### Need to buy 🛒

| Qty | Component | Search on ML |
|-----|-----------|-------------|
| 4 | PC817C DIP-4 | "PC817C DIP" |
| 4 | Diodo 1N4007 | "1N4007" |
| 1 | LED verde 5mm | (or from Arduino kit) |
| 1 | LED vermelho 5mm | |
| 1 | LED azul 5mm | |
| 1 | LED laranja/amarelo 5mm | |
| 1 | Buzzer ativo 3V | "buzzer ativo 3V" |
| 1 | CR2032 | farmácia |
| 1 | Suporte CR2032 | "suporte CR2032" |
| 1 | Placa perfurada | "placa perfurada 5x7" |
| 1 | Plugue macho 3 pinos 10A NBR 14136 | qualquer elétrica |

**Total estimated cost:** ~R$30

---

## Wiring Notes

### Critical connections:

1. **Battery − must connect to TERRA pin** permanently (one jumper wire)
   - This breaks Canal C isolation but TERRA is safe earth ground
   - Required for buzzer circuit to have return path
   - Canals A and B remain properly isolated

2. **Antiparallel diode** in each canal (across PC817 LED):
   - Protects internal LED from reverse voltage
   - Allows circuit to work regardless of plug orientation
   - Without it: LED destroyed by 180V reverse on negative half cycle

3. **PC817 polarity**:
   - Pin 1 (anode) → connects to AC side via resistors + 1N4007
   - Pin 2 (cathode) → connects to NEUTRO (canals A/B) or TERRA (canals C1/C2)
   - Pin 4 (collector) → indicator LED cathode (DC side)
   - Pin 3 (emitter) → GND (DC side)

### Why two diodes per canal:

| Diode | Job |
|-------|-----|
| **1N4007 (series)** | Allows current only one direction; blocks reverse |
| **Antiparallel** | Clamps reverse voltage across PC817 LED to 0.7V (safe) |

---

## Physical Construction

### Suggested enclosure:

Buy a **plugue macho 3 pinos 10A NBR 14136** with a body that opens. Mount the circuit inside, with the 4 LEDs poking through holes drilled in the housing.

### Layout strategy:

```
┌─────────────────────────┐
│ 🟢  🔴  🔵  🟠         │  ← LEDs visible
│                         │
│ [circuit + buttons]     │
│                         │
│ [CR2032]                │
│                         │
│  ■   ■                  │  ← flat pins
│    ●                    │  ← earth pin
└─────────────────────────┘
```

### Order of soldering:

1. Resistors (lowest profile)
2. 1N4007 diodes
3. PC817 chips
4. Push buttons
5. Battery holder
6. LEDs last (adjust height for enclosure alignment)

### Three wires to plug pins:

- 🔴 Red wire → flat pin 1
- 🔵 Blue wire → flat pin 2
- 🟢 Green wire → round earth pin

---

## Key Concepts Explained

### Why FASE and NEUTRO orientation doesn't matter:

AC alternates 50 times per second. The circuit works on different half cycles depending on orientation:

| Pino 1 | Pino 2 | LED fires on |
|--------|--------|-------------|
| FASE | NEUTRO | positive half cycle |
| NEUTRO | FASE | negative half cycle |

Either way the LED fires 25 times per second — appears constantly on.

### Why the resistors don't need to be on both sides:

Series circuit: current is the same everywhere. Whether the resistors are before or after the LED, they limit the current to the same value.

```
127V → [40kΩ] → [LED] → 0V    same as    127V → [LED] → [40kΩ] → 0V
```

Both topologies give:
- 125.5V across resistors
- 1.5V across LED
- ~1.4mA current

### Why permanent Battery− to TERRA is safe:

- TERRA is earth ground (0V reference)
- The optocouplers still isolate Canals A and B properly
- Canal C loses isolation but TERRA is safe potential
- Buzzer circuit needs this connection for return path
- No DC path exists from Battery+ to flat pins (buttons are normally open)
- 127V AC on flat pins is blocked from reaching battery by opto isolation

---

## Files Generated During Design

1. `layout_tester.svg` — initial 3-canal design
2. `layout_tester_v2.svg` — dual-path Canal C
3. `layout_tester_v3.svg` — 4-canal with polarity detection
4. `layout_tester_final.svg` — simplified 3-LED final
5. `layout_tester_buzzer.svg` — added buzzer + 2 buttons
6. `pc817_pin1.svg` — pin 1 identification guide

---

## Final Notes

The design is fully passive (no microcontroller), reliable, and uses components mostly already in the Arduino kit. The whole thing fits inside a standard 3-pin plug body.

**Total components needed:** ~25 items
**Estimated cost:** ~R$30 for what's missing
**Build time:** 1-2 hours
**Skill level:** Intermediate (basic soldering on perfboard)

Boa sorte com o projeto! 😄
