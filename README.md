# BGSI Enchant Bot (Windows)

Uses Shadow Crystals in *Bubble Gum Simulator Infinity* until every equipped pet has the enchants you pick (e.g. **Ultra Roller + Secret Hunter**). It reads the screen and clicks with the normal mouse input. It does not touch the game files.

Use at your own risk: automating Roblox can break its Terms of Use.

## Setup

1. Install [Python 3.12+](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"**.
2. Download this repo (`git clone` or **Code → Download ZIP**).
3. Run **`setup.bat`**. It installs the packages, offers to install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) and checks everything with `doctor`.

## Use

1. Open BGSI in Roblox with a big window (maximised or F11) and open the enchant book (**Select Pet**).
2. Run **`start.bat`**, pick your two enchants, press **Test detection**, then **Start**.
3. **F8** stops the bot, and so does moving your mouse.

Per pet the bot locks the first wanted enchant, uses crystals until the second one appears, then moves on. Super Legendary pets are skipped, and wanted enchants are never rerolled. Moving or resizing the window is handled automatically.

## Settings

Saved in `config.json` (most are also in the control panel):

- `targets`: your two enchants
- `max_crystals_total`: stop after this many crystals (0 = no limit)
- `only_pets`: grid positions to do, e.g. `[1, 2, 5]` (empty = all)
- `stop_hotkey`: default `F8`
- `tesseract_path`: path to `tesseract.exe` if it is not found

Click timings are documented in `bgsi_enchanter/config.py`.

## Commands

`run.bat doctor` (check setup), `run.bat analyze` (show what it sees, no clicks), `run.bat mousetest`, `run.bat probe`, `run.bat run` (without the control panel).

## Troubleshooting

- **Black screenshot:** use windowed/borderless instead of exclusive fullscreen and turn off HDR.
- **Clicks do nothing:** don't run Roblox as administrator, or run the bot as administrator too.
- **Something goes wrong:** check `debug/last_run.log` and the error screenshots in `debug/`.

Made with the help of AI.
