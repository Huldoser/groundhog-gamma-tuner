from dashboard import TunerDashboard


if __name__ == "__main__":
    try:
        TunerDashboard().run()
    except KeyboardInterrupt:
        print("\nProgram interrupted and exiting cleanly...")
