"""PyQt5 대시보드 런처 — 더블클릭 실행용."""
import sys
import traceback


def main():
    try:
        from PyQt5.QtWidgets import QApplication
        from fang_v10.dashboard import Dashboard
    except ImportError as e:
        print("=" * 50)
        print("필수 패키지가 설치되지 않았습니다.")
        print(f"오류: {e}")
        print()
        print("아래 명령어로 설치해주세요:")
        print("  pip install -r fang_v10/requirements.txt")
        print("=" * 50)
        input("\nEnter 키를 누르면 종료됩니다...")
        sys.exit(1)

    try:
        app = QApplication(sys.argv)
        window = Dashboard()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        print("=" * 50)
        print("대시보드 실행 중 오류 발생:")
        print("=" * 50)
        traceback.print_exc()
        print()
        input("Enter 키를 누르면 종료됩니다...")
        sys.exit(1)


if __name__ == "__main__":
    main()
