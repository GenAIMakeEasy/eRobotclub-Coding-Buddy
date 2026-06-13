# R Block Programming Interface — Block Catalogue

> **How to use this file:**  
> This is the reference catalogue for the `ollo-r-block-assistant.md` skill.  
> Upload **both files** together in the same chat session.  
> The AI will consult this file whenever it needs the exact wording of a block.  
> Do not upload this file alone — it works together with the skill file.

---

This document provides a precise mapping of the text strings and elements present inside each category tab of the R Block coding app (used with ROBOTIS R+ Ollo series robots), strictly reflecting the visual layout of the interface blocks.

---

## Slot type legend

* **Hexagonal Condition Slot (Boolean Input):** A pointed six-sided input receptacle designed exclusively to accept true/false statements (e.g., comparison blocks or boolean switches). Blocks with matching pointed ends snap natively into these zones to govern loop control and branching logic.
* **Nested Block Sequence Area (C-Block Slot / Mouth):** An open physical inlet configuration that wraps around a sequential, vertically stackable pile of standard executable command blocks. This designates the localized code segment that runs iteratively during loops or executes sequentially during true conditional evaluations.
* **Embedded Dropdown Menu Selector `[Dropdown]`:** A functional text element block providing interactive pre-defined option arrays (e.g., choosing motor channels `No.1` vs `No.2`, or operational directions).
* **Numeric Input Field / Standalone Value Block `[Value]`:** Circular or oval-shaped parameters accepting constant numeric attributes, manual numbers, typed text strings, or rounded reporter variables.

---

## Control

### Repeat until
* **Exact Text:** `Repeat until`
* **Remarks:** Contains a hexagonal condition slot followed by a nested block sequence area.

### Wait while
* **Exact Text:** `Wait while`
* **Remarks:** Contains a hexagonal condition slot.

### Wait until
* **Exact Text:** `Wait until`
* **Remarks:** Contains a hexagonal condition slot.

### Stop Repeat
* **Exact Text:** `Stop Repeat`
* **Remarks:** Standard inline text block.

### End Program
* **Exact Text:** `End Program`
* **Remarks:** Standard inline text block.

### No.1 Block
* **Exact Text:** `[Dropdown] Block`
* **Remarks:** Includes an embedded dropdown field defaulted to `No.1`. Options 1 to 5.

### Jump to block
* **Exact Text:** `Jump to [Dropdown] block`
* **Remarks:** Includes an embedded dropdown field defaulted to `No.1`. Options 1 to 5.

### Forever
* **Exact Text:** `Forever`
* **Remarks:** Contains a nested block sequence area.

### Repeat time(s)
* **Exact Text:** `Repeat [Value] time(s)`
* **Remarks:** Includes an embedded numeric field defaulted to `10`, followed by a nested block sequence area.

### Wait seconds
* **Exact Text:** `Wait [Value] seconds`
* **Remarks:** Includes an embedded numeric field defaulted to `1`.

### if then
* **Exact Text:** `[Icon] if [Condition] then`
* **Remarks:** Features a blue configuration gear icon, a hexagonal condition slot, and a nested block sequence area. The gear icon expands the block to add an `else` branch.

### Repeat while
* **Exact Text:** `Repeat while`
* **Remarks:** Contains a hexagonal condition slot followed by a nested block sequence area.

---

## Calculation

### Comparison Operator
* **Exact Text:** `[Value] [Dropdown] [Value]`
* **Remarks:** Pointed boolean block shape featuring an initial numeric input field (defaulted to `0`), a comparison operator dropdown menu (defaulted to `=`), and a trailing numeric input field (defaulted to `0`).

### Arithmetic Operator
* **Exact Text:** `[Value] [Dropdown] [Value]`
* **Remarks:** Rounded value block shape featuring an initial numeric input field (defaulted to `0`), an arithmetic operator dropdown menu (defaulted to `+`), and a trailing numeric input field (defaulted to `0`).

### Logical And / Or
* **Exact Text:** `[Condition Slot] [Dropdown] [Condition Slot]`
* **Remarks:** Pointed boolean block shape featuring two hexagonal logical condition slots separated by a dropdown menu selector defaulted to `and`.

### Logical Not
* **Exact Text:** `Not [Condition Slot]`
* **Remarks:** Pointed boolean block shape featuring a single hexagonal logical condition slot.

### Standalone Numeric Value
* **Exact Text:** `0`
* **Remarks:** Small standalone rounded value block defaulted to `0`.

### Standalone Text String
* **Exact Text:** `Text`
* **Remarks:** Standalone rounded value block defaulted to the string literal `Text`.

### Standalone Degree Value
* **Exact Text:** `0°`
* **Remarks:** Standalone rounded angle/degree value block defaulted to `0°`.

### Boolean State Constant
* **Exact Text:** `[Dropdown]`
* **Remarks:** Pointed boolean block shape featuring a single dropdown selector defaulted to `True`.

### Random number between 0 and
* **Exact Text:** `Random number between 0 and [Value]`
* **Remarks:** Elongated rounded block featuring a trailing numeric input field defaulted to `100`.

### Join text strings
* **Exact Text:** `Join [Value] and [Value]`
* **Remarks:** Rounded block shape containing an initial text input field (defaulted to `Text`) and a secondary trailing value field (defaulted to `1`).

### Modulo / Rest of
* **Exact Text:** `Rest of [Value] / [Value]`
* **Remarks:** Rounded block shape containing an initial numeric field (defaulted to `10`) and a secondary trailing numeric field (defaulted to `2`).

### length of string
* **Exact Text:** `length of [Value]`
* **Remarks:** Rounded block shape featuring an internal text input tracking field defaulted to `Text`.

### Letter of string
* **Exact Text:** `Letter [Value] of [Value]`
* **Remarks:** Rounded block shape containing an initial character/text parameter field (defaulted to `Text`) and a secondary trailing position index field (defaulted to `1`).

---

## Variable

* **Special Constraint:** This tab contextually changes and exposes its block subset **only after** clicking the gray `Create a variable` helper utility button and defining a custom label.

### Create a variable
* **Exact Text:** `Create a variable`
* **Remarks:** Gray rectangular configuration trigger button.

### Define variable as
* **Exact Text:** `Define [Dropdown] as [Value Slot]`
* **Remarks:** Variable assignment block featuring a dropdown selection menu defaulted to `variable_1` and a blank tracking slot.

### Add the variable by
* **Exact Text:** `Add the variable [Dropdown] by [Value]`
* **Remarks:** Incremental assignment block with a variable dropdown menu (`variable_1`) and an oval numeric entry field defaulted to `1`.

### variable_1 (oval reference)
* **Exact Text:** `[Dropdown]`
* **Remarks:** Standalone oval variable tracking reference parameter block with a dropdown selector tracking active variables, defaulted to `variable_1`.

---

## Function

* **Special Constraint:** This tab contextually changes and exposes its block subset **only after** clicking the gray `Create a function` helper utility button and defining a function signature name.

### Create a function
* **Exact Text:** `Create a function`
* **Remarks:** Gray rectangular macro creation utility button.

### Run function
* **Exact Text:** `Run function " [Dropdown] "`
* **Remarks:** Function wrapper header displaying a dropdown tracking active procedures (defaulted to `" abd "`) followed by an open nested block sequence area underneath.

### Call function
* **Exact Text:** `Call function " [Dropdown] "`
* **Remarks:** Stackable runtime trigger block containing an internal macro definition selection dropdown menu defaulted to `" abd "`.

### Return
* **Exact Text:** `Return`
* **Remarks:** Standard flat-ended function termination and parameter escape inline block.

---

## Sensing

### If the IR sensor value
* **Exact Text:** `If the [Dropdown] IR sensor value [Dropdown] [Value]`
* **Remarks:** Features two dropdown menus (defaulted to `No.1` and `>`) and an embedded numeric field (defaulted to `200`).

### IR sensor value
* **Exact Text:** `[Dropdown] IR sensor value`
* **Remarks:** Features a starting dropdown field defaulted to `No.1`.

### If detected sound count
* **Exact Text:** `If [Dropdown] detected sound count [Dropdown] [Value]`
* **Remarks:** Features two dropdown menus (defaulted to `Final` and `>`) and an embedded numeric field (defaulted to `0`).

### detected sound count
* **Exact Text:** `[Dropdown] detected sound count`
* **Remarks:** Features a starting dropdown field defaulted to `Final`.

### Reset final sound count
* **Exact Text:** `Reset final sound count`
* **Remarks:** Standard standalone inline text block.

### Was the remote control signal received?
* **Exact Text:** `Was the remote control signal received?`
* **Remarks:** Standalone question text block.

### If the remote signal is
* **Exact Text:** `If the remote signal is [Dropdown]`
* **Remarks:** Features a trailing dropdown menu selector defaulted to `Up`.

### Remote signal value
* **Exact Text:** `Remote signal value`
* **Remarks:** Standard standalone text block.

### Number of times the start button is pressed
* **Exact Text:** `Number of times the start button is pressed`
* **Remarks:** Standard standalone text block.

### If the start button is pressed
* **Exact Text:** `If the start button is pressed [Dropdown] [Value] times`
* **Remarks:** Features a dropdown comparison menu (defaulted to `==`) and a numeric input field (defaulted to `1`).

### timer value (oval)
* **Exact Text:** `[Dropdown] timer value`
* **Remarks:** Features a starting dropdown field defaulted to `Increase`.

### Reset timer value
* **Exact Text:** `Reset [Dropdown] timer value`
* **Remarks:** Features an embedded dropdown configuration menu defaulted to `Increase`.

---

## Motion

### Move robot's
* **Exact Text:** `Move robot's [Dropdown] [Dropdown] at speed [Value]`
* **Remarks:** Features two dropdown menus (defaulted to `Back axis` and `Forward`) and an embedded numeric input field (defaulted to `50`).

### Stop robot
* **Exact Text:** `Stop robot`
* **Remarks:** Standard standalone inline text block.

### Rotate
* **Exact Text:** `Rotate in [Dropdown] the back axis of [Dropdown] at speed [Value] .`
* **Remarks:** The first dropdown is the rotation orientation, usually `clockwise` or `counterclockwise`. The second dropdown selects the side, usually `Left` or `Right`. The block concludes with a trailing period.

### Stop motor
* **Exact Text:** `Stop [Dropdown] motor`
* **Remarks:** Features an embedded dropdown field defaulted to `Left`.

### Turn LEDs
* **Exact Text:** `Turn [Dropdown] LEDs [Dropdown]`
* **Remarks:** Features two text dropdown configuration menus (defaulted to `Both` and `on`).

### Set auto-shutdown time to
* **Exact Text:** `Set auto-shutdown time to [Value] minutes`
* **Remarks:** Features an embedded numeric input field defaulted to `5`.

### Position value of Motor
* **Exact Text:** `Position value of [Dropdown] Motor`
* **Remarks:** Rounded block shape featuring an internal dropdown field defaulted to `No.1`.

### Turn torque for motor
* **Exact Text:** `Turn [Dropdown] torque for motor [Dropdown] .`
* **Remarks:** Features two dropdown configuration fields (defaulted to `on` and `No.1`) and concludes with a trailing period.

### Control both motors at the same speed
* **Exact Text:** `Control both motors at the same speed [Dropdown]`
* **Remarks:** Features a trailing dropdown menu selector defaulted to `Set`.

### Set motor to mode
* **Exact Text:** `Set [Dropdown] motor to [Dropdown] mode`
* **Remarks:** Features two configuration dropdown selection fields (defaulted to `No.1` and `Wheel`).

### Motor Single Drive Control
* **Exact Text:** `[Dropdown] motor [Dropdown] [Dropdown] at a speed [Value] .`
* **Remarks:** Features three text dropdown adjustment segments (defaulted to `No.1`, `Rotate`, and `Clockwise`), followed by a numeric input speed field (defaulted to `50`), and concludes with a trailing period.

### Motor Degree Path Control
* **Exact Text:** `[Dropdown] motor move to the [Value] degree position at a speed [Value] .`
* **Remarks:** Features a starting channel dropdown (defaulted to `No.1`), a degree target numeric input field (defaulted to `0°`), an execution speed field (defaulted to `50`), and concludes with a trailing period.

### Turn the motor's torque limit
* **Exact Text:** `Turn [Dropdown] the motor's torque limit`
* **Remarks:** Features an initial activation toggle dropdown menu selector defaulted to `on`.

---

## Screen

### Output value to screen
* **Exact Text:** `Output value [Value] to screen`
* **Remarks:** Standard inline stackable command block featuring an oval numeric/string parameter input field defaulted to `0`.

### Output value to screen and line break
* **Exact Text:** `Output value [Value] to screen and line break`
* **Remarks:** Standard inline stackable command block featuring an oval numeric/string parameter input field defaulted to `0`, appending a trailing newline command to the hardware display layout.

---

## Sound

### Play melody
* **Exact Text:** `Play melody [Dropdown]`
* **Remarks:** Standard inline stackable command block featuring an embedded melody index selection dropdown menu defaulted to `No.1`.

### Play melody and wait
* **Exact Text:** `Play melody [Dropdown] and wait`
* **Remarks:** Standard inline stackable command block featuring an embedded melody index selection dropdown menu defaulted to `No.1`.

### Play the note of the octave for seconds
* **Exact Text:** `Play the [Dropdown] note of the [Dropdown] octave for [Value] seconds`
* **Remarks:** Features a musical note pitch selection dropdown (defaulted to `Do`), a pitch register octave selection dropdown (defaulted to `4`), and an oval numeric duration input field defaulted to `0.2`.

---

## Low-Level Bus Matrix Registers

### Stack Register Block
* **Exact Text:**
  ```
  ID      [Value]
  ADDR    [Value]
  LENGTH  [Value]
  PARA    [Value]
  ```
* **Remarks:** Advanced low-level block — generally not needed for student-level programs. Avoid unless explicitly required.
