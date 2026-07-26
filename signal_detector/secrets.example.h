#pragma once
// Template. Copy this to secrets.h and fill in your own values.
// secrets.h is git-ignored; this example file is safe to commit.

#define WIFI_SSID   "your-wifi-ssid"
#define WIFI_PASS   "your-wifi-password"

#define BOARD_ID    1                  // set to 2 on the second board
#define SERVER_HOST "192.168.1.50"     // your phone's LAN IP (Termux: ifconfig)
#define SERVER_PORT 8000

// Optional AD8317/AD8318 RF power detector (catches cellular uplink bursts).
// 0 = no module attached (the board just doesn't report an "rf" field).
#define RF_ENABLED  0
#define RF_ADC_PIN  4                  // must be ADC1 on the S3 (GPIO1..GPIO10);
                                       // ADC2 pins do not work while WiFi is on.
#define RF_INVERTED 1                  // 1 = bare AD8317/AD8318 (more RF power -> LOWER volts).
                                       // Set 0 if your module has an output stage that
                                       // inverts the slope. Check it: see the manual.

// Optional hunt mode: park this board on one MAC's WiFi channel and drive a buzzer
// by proximity, so you can carry it and walk in on the phone. The target is chosen
// from the collector's web page; the board picks it up in the reply to its next POST.
// Only worth enabling on the board you intend to carry.
#define HUNT_ENABLED    0
#define HUNT_BUZZER_PIN 5              // passive piezo buzzer: + to this pin, - to GND
#define HUNT_ZERO_PIN   0              // push-button to GND; GPIO0 is the BOOT button, so
                                       // this works with no extra wiring. Tap = re-zero
                                       // (one software attenuator step), hold 1 s = full
                                       // scale back.
