"""PyQt5 dashboard launcher."""
import os
import sys
import traceback

# Ensure parent directory is in sys.path so 'fang_v10' package is importable
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)


def main():
    try:
        from PyQt5.QtWidgets import QApplication
        from fang_v10.dashboard import Dashboard
    except ImportError as e:
        print("=" * 50)
        print("Missing packages. Run this command first:")
        print()
        print("  py -m pip install PyQt5 ccxt pandas matplotlib")
        print()
        print(f"Error: {e}")
        print("=" * 50)
        input("\nPress Enter to exit...")
        sys.exit(1)

    try:
        app = QApplication(sys.argv)
        window = Dashboard()
        window.show()
        sys.exit(app.exec_())
    except Exception:
        print("=" * 50)
        print("Dashboard error:")
        print("=" * 50)
        traceback.print_exc()
        print()
        input("Press Enter to exit...")
        sys.exit(1)


if __name__ == "__main__":
    main()
