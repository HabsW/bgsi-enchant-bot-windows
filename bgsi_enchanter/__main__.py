"""bgsi_enchanter command line (Windows).

  start.bat            open the control panel
  run.bat run          run the bot in this console (Ctrl+C or F8 stops it)
  run.bat analyze      look at the game once and say what it would do (no clicks)
  run.bat doctor       check Python packages, Tesseract, window and capture
  run.bat mousetest    check pointer accuracy over the Roblox window (no clicks)
  run.bat probe        show the detected Roblox window
"""
import argparse
import sys

from .config import clean_targets, load_config, save_config


def main(argv=None):
    # A non-UTF-8 console must not crash on odd characters.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):       # pythonw: no console at all
            pass
    if sys.platform == "win32":
        try:
            from . import win  # noqa: F401  (sets per-monitor DPI awareness before anything else)
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="run.bat", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", nargs="?", default="gui",
                   choices=["gui", "run", "analyze", "doctor", "mousetest", "probe"])
    p.add_argument("--targets", help='e.g. "Ultra Roller,Secret Hunter" (saved to config.json)')
    args = p.parse_args(argv)

    cfg = load_config()
    if args.targets:
        cfg["targets"] = clean_targets(args.targets.split(","))
        save_config(cfg)

    if args.cmd == "gui":
        from .gui import main as gui_main
        return gui_main()
    if args.cmd == "doctor":
        from .diag import doctor
        return 0 if doctor(cfg) else 1
    if args.cmd == "probe":
        from .diag import probe
        probe(cfg)
        return 0
    if args.cmd == "mousetest":
        from .diag import mouse_test
        return 0 if mouse_test(cfg) else 1

    from .bot import Bot
    bot = Bot(cfg, dry_run=(args.cmd == "analyze"))
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
        print("\nStopped (Ctrl+C).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
