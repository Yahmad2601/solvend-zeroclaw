// SolVend ESP32 firmware — serial bridge build.
//
// The ESP32 no longer talks to the internet. It has no Wi-Fi credentials, no
// API key, and no TLS stack. It is a dumb peripheral: it reports keypresses and
// obeys dispense commands. Every decision about whether a code is valid happens
// on the Pi, in SQL, in code the language model cannot reach.
//
// That is a real security improvement over the Vercel build, and worth saying
// in the write-up: the credential that used to sit in flash on a device sitting
// in a public shop no longer exists. Pull the ESP32 out of the machine and
// dump it and you get pin numbers.
//
// WIRE PROTOCOL (115200 8N1, newline-terminated, ASCII)
//   ESP32 -> Pi   KEYPAD:1234              four digits, user pressed #
//                 EVENT:BOOT               firmware started / watchdog reset
//                 EVENT:DISPENSED:drink-2  gantry cycle completed
//                 EVENT:ERROR:<reason>
//   Pi -> ESP32   DISPENSE:drink-1|drink-2|drink-3
//                 DENY:<short reason>      shown on the LCD, then reset
//
// TRUST BOUNDARY: anything able to write /dev/ttyUSB0 can dispense. That is
// physical/root access to the Pi, which is already game over. Documented, not
// mitigated.

// --- LIBRARIES ---
#include <LiquidCrystal_I2C.h> // For the LCD
#include <Keypad.h>            // For the Keypad
#include <ESP32Servo.h>        // For the Servo Motors

// --- 1. SERIAL PROTOCOL CONFIG ---
const unsigned long VERIFY_TIMEOUT_MS = 10000; // Pi must answer within 10s

// --- 2. HARDWARE PIN DEFINITIONS (UNCHANGED) ---

// LCD Display (16x2 with I2C)
LiquidCrystal_I2C lcd(0x27, 16, 2);

// Keypad (4x3)
const byte ROWS = 4;
const byte COLS = 3;
char keys[ROWS][COLS] = {
  {'1','2','3'},
  {'4','5','6'},
  {'7','8','9'},
  {'*','0','#'} // '*' is Clear, '#' is Enter
};
byte rowPins[ROWS] = {13, 12, 14, 27}; // Connect to R1, R2, R3, R4
byte colPins[COLS] = {26, 25, 33};     // Connect to C1, C2, C3
Keypad keypad = Keypad(makeKeymap(keys), rowPins, colPins, ROWS, COLS);

// Buzzer
const int BUZZER_PIN = 2;
const int BUZZER_FREQ = 1500; // Tone frequency in Hz
const int BUZZER_DURATION = 75; // Tone duration in ms

// Stepper Motor (from Gantry)
const int STEPPER_STEP_PIN = 17;  // Pin 17 for A4988 'STEP'
const int STEPPER_DIR_PIN  = 16;  // Pin 16 for A4988 'DIR'

// Servo Motors (Merged)
const int SERVO_BASE_PIN = 18; // Gantry rotation (from Gantry)
const int SERVO_PIN_1    = 15; // For drinkId 'drink-1'
const int SERVO_PIN_2    = 4;  // For drinkId 'drink-2'
const int SERVO_PIN_3    = 5;  // For drinkId 'drink-3'

Servo servoBase;    // (from Gantry)
Servo servoItem1;   // Renamed from servo1
Servo servoItem2;   // Renamed from servo2
Servo servoItem3;   // Renamed from servo3

// --- 3. POSITIONS & GLOBAL VARIABLES (UNCHANGED) ---

const int SERVO_1_2_IDLE_POS = 0;   // Idle position for servos 1 & 2
const int SERVO_1_2_DISPENSE_POS = 90;  // Dispense position for servos 1 & 2

const int SERVO_3_IDLE_POS = 90;  // Idle position for servo 3
const int SERVO_3_DISPENSE_POS = 0;   // Dispense position for servo 3

const int BASE_HOME_POS = 0;    // Home position for base servo
const int BASE_DISPENSE_POS = 180; // Dispense position for base servo

// Stepper Positions (from Gantry)
const int CHECKOUT_POS = 0;   // The drop-off position (absolute)
const int ITEM_1_POS = 1100;  // Absolute steps to Item 1
const int ITEM_2_POS = 650;   // Absolute steps to Item 2
const int ITEM_3_POS = 250;   // Absolute steps to Item 3

int currentStepperPos = 0; // Tracks the gantry's position (from Gantry)

// Global Variables
String enteredOtp = "";
bool isVerifying = false;          // waiting on the Pi; keypad is ignored
unsigned long verifyStartedMs = 0; // for the timeout
String serialBuf = "";             // inbound line assembly

// --- HELPER FUNCTIONS ---

// Displays messages on the LCD. NOTE: the "LCD > " serial echo from the old
// build is deliberately gone — Serial is now a control channel, not a debug
// console, and the daemon should not have to parse around chatter. The Pi
// keeps the real log (`journalctl -u solvend-serial`).
void displayMessage(String line1, String line2, int delay_ms = 0) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1.substring(0, 16)); // Ensure text fits
  lcd.setCursor(0, 1);
  lcd.print(line2.substring(0, 16)); // Ensure text fits
  if (delay_ms > 0) {
    delay(delay_ms);
  }
}

// Resets the input and LCD to the initial state
void resetInputState() {
  enteredOtp = "";
  isVerifying = false;
  displayMessage("Enter OTP:", "****");
}

// --- SETUP FUNCTION ---
void setup() {
  Serial.begin(115200);

  // Hardware Initialization
  lcd.init();
  lcd.backlight();
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(STEPPER_STEP_PIN, OUTPUT);
  pinMode(STEPPER_DIR_PIN, OUTPUT);

  // Allocate timers for 4 ESP32 servos
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  // Attach servos and set to home position
  servoBase.attach(SERVO_BASE_PIN);
  servoItem1.attach(SERVO_PIN_1);
  servoItem2.attach(SERVO_PIN_2);
  servoItem3.attach(SERVO_PIN_3);

  servoBase.write(BASE_HOME_POS);
  servoItem1.write(SERVO_1_2_IDLE_POS);   // Set servo 1 to idle (0)
  servoItem2.write(SERVO_1_2_IDLE_POS);   // Set servo 2 to idle (0)
  servoItem3.write(SERVO_3_IDLE_POS);     // Set servo 3 to its idle (90)

  // Tell the daemon we (re)started, so it can log a reset and clear any
  // in-flight expectation from a previous life.
  Serial.println("EVENT:BOOT");

  resetInputState(); // Show "Enter OTP:"
}

// --- MAIN LOOP ---
void loop() {
  pollSerial();   // MUST run before the isVerifying guard — the whole point of
                  // being "verifying" is that we are waiting for a serial reply.

  // If the Pi never answers, do not strand the machine holding a dead OTP.
  if (isVerifying && (millis() - verifyStartedMs > VERIFY_TIMEOUT_MS)) {
    Serial.println("EVENT:ERROR:pi_timeout");
    displayMessage("System Busy", "Try again", 2500);
    resetInputState();
    return;
  }

  if (isVerifying) return; // Ignore keypad if busy

  char key = keypad.getKey();

  if (key) {
    // Sound the buzzer on any key press
    tone(BUZZER_PIN, BUZZER_FREQ, BUZZER_DURATION);

    if (key == '#') { // Enter key
      if (enteredOtp.length() == 4) {
        verifyOTP(enteredOtp);
      } else {
        displayMessage("Invalid OTP", "Must be 4 digits", 2000);
        resetInputState();
      }
    } else if (key == '*') { // Clear key
      resetInputState();
    } else if (isDigit(key) && enteredOtp.length() < 4) {
      enteredOtp += key;
      lcd.setCursor(enteredOtp.length() - 1, 1);
      lcd.print(key); // Show the entered digit
    }
  }
}

// --- OTP SUBMISSION (was: HTTP verification) ---
// No network, no JSON, no decision. Report the keypress and wait.
void verifyOTP(String otp) {
  isVerifying = true;
  verifyStartedMs = millis();
  displayMessage("Verifying OTP...", "");
  Serial.println("KEYPAD:" + otp);
}

// --- SERIAL COMMAND HANDLING ---
void pollSerial() {
  while (Serial.available()) {
    char c = (char) Serial.read();
    if (c == '\n') {
      String line = serialBuf;
      serialBuf = "";
      handleSerialLine(line);
      return;             // handle one command per loop; dispense blocks anyway
    } else if (c != '\r') {
      if (serialBuf.length() < 64) serialBuf += c;   // bounded: no heap blowup
      // silently drop overlong garbage rather than acting on a truncated command
    }
  }
}

void handleSerialLine(String line) {
  line.trim();
  if (line.length() == 0) return;

  if (line.startsWith("DISPENSE:")) {
    String drinkId = line.substring(9);
    drinkId.trim();
    displayMessage("Success!", "Dispensing...");
    dispenseDrink(drinkId);
    resetInputState(); // Reset for next customer

  } else if (line.startsWith("DENY:")) {
    String reason = line.substring(5);
    reason.trim();
    if (reason.length() == 0) reason = "Try again";
    tone(BUZZER_PIN, 400, 400);          // low buzz = rejected
    displayMessage("Invalid Code", reason.substring(0, 16), 3000);
    resetInputState();

  } else if (line == "PING") {
    Serial.println("EVENT:PONG");        // daemon liveness check
  }
  // Unknown lines are ignored on purpose. Never guess at a malformed command
  // on the wire that controls a motor.
}

// =========================================================
// --- DISPENSE LOGIC (UNCHANGED) ---
// =========================================================

/**
 * @brief Router: maps a slot id from the Pi to the correct Gantry function.
 */
void dispenseDrink(String drinkId) {
  if (drinkId == "drink-1") {
    dispenseItem(1, ITEM_1_POS, servoItem1);
    Serial.println("EVENT:DISPENSED:drink-1");

  } else if (drinkId == "drink-2") {
    dispenseItem(2, ITEM_2_POS, servoItem2);
    Serial.println("EVENT:DISPENSED:drink-2");

  } else if (drinkId == "drink-3") {
    dispenseItem(3, ITEM_3_POS, servoItem3);
    Serial.println("EVENT:DISPENSED:drink-3");

  } else {
    Serial.println("EVENT:ERROR:unknown_drink");
    displayMessage("Error:", "Not in Stock");
    delay(2000); // Give time to read error
  }
}

/**
 * @brief Main dispense sequence (from Gantry code).
 * This function controls the *physical* gantry movement.
 */
void dispenseItem(int itemNumber, int targetSteps, Servo& itemServo) {

  // --- STEP 1: Move Gantry Anti-Clockwise to Item ---
  moveStepperTo(targetSteps, "Gantry to Item " + String(itemNumber));
  delay(500); // Pause at item

  // --- STEP 2: Dispense Item & Return ---
  if (itemNumber == 3) {
    // --- Logic for Item 3 (Idle 90, Dispense 0) ---
    displayMessage("Dispensing Item", "Servo 3 -> 0");
    itemServo.write(SERVO_3_DISPENSE_POS); // Move to 0 to dispense
    delay(1000); // Pause for 1 second for item to drop

    displayMessage("Securing Item...", "Servo 3 -> 90");
    itemServo.write(SERVO_3_IDLE_POS); // Return to idle (90)
    delay(500); // Wait for servo to return before moving

  } else {
    // --- Logic for Items 1 & 2 (Idle 0, Dispense 90) ---
    displayMessage("Dispensing Item", "Servo " + String(itemNumber) + " -> 90");
    itemServo.write(SERVO_1_2_DISPENSE_POS); // Move to 90 to dispense
    delay(1000); // Pause for 1 second for item to drop

    displayMessage("Securing Item...", "Servo " + String(itemNumber) + " -> 0");
    itemServo.write(SERVO_1_2_IDLE_POS); // Return to idle (0)
    delay(500); // Wait for servo to return before moving
  }

  // --- STEP 3: Move Gantry Clockwise to Checkout ---
  moveStepperTo(ITEM_3_POS, "Gantry to C/Out");
  // NOTE (unchanged from your build): this targets ITEM_3_POS, not CHECKOUT_POS.
  // Kept verbatim because it is what your machine is calibrated against —
  // flagging it only so you know it is deliberate and not a refactor slip.

  // --- STEP 4: Rotate Base Servo to Dispense (New Timing) ---
  delay(500); // Pause for 0.5 seconds *before* rotating

  displayMessage("Dispensing...", "Base -> 180");
  servoBase.write(BASE_DISPENSE_POS);
  delay(5000); // Pause for 5 seconds

  // --- STEP 5A: Return Base Servo to Home ---
  displayMessage("Resetting...", "Base -> 0");
  servoBase.write(BASE_HOME_POS); // Base returns to 0
  delay(1000); // Wait for base servo to get home
  // --- STEP 5B: Move Gantry Clockwise to Initial ---
  moveStepperTo(CHECKOUT_POS, "Gantry to Init");

  // --- STEP 6: Signal Completion ---
  displayMessage("Complete!", "Item " + String(itemNumber) + " Ready");
  tone(BUZZER_PIN, BUZZER_FREQ, 150);
  delay(200); // Short pause between beeps
  tone(BUZZER_PIN, BUZZER_FREQ, 150);
  delay(2000); // Wait 2 seconds before resetting LCD
}

/**
 * @brief Moves the stepper gantry and updates the LCD (from Gantry code).
 */
void moveStepperTo(int absoluteTargetSteps, String lcdMessage) {

  displayMessage("Moving Gantry...", lcdMessage);

  int stepsToMove = absoluteTargetSteps - currentStepperPos;

  // Set direction
  if (stepsToMove > 0) {
    digitalWrite(STEPPER_DIR_PIN, HIGH); // ANTI-CLOCKWISE
  } else {
    digitalWrite(STEPPER_DIR_PIN, LOW);  // CLOCKWISE
  }

  // Pulse the step pin
  for (int i = 0; i < abs(stepsToMove); i++) {
    digitalWrite(STEPPER_STEP_PIN, HIGH);
    delayMicroseconds(700); // Controls speed. Lower = faster.
    digitalWrite(STEPPER_STEP_PIN, LOW);
    delayMicroseconds(700);
  }

  // Update our position
  currentStepperPos = absoluteTargetSteps;
}
