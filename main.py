from gui import BitaxeAutotuningApp


if __name__ == "__main__":
    try:
        app = BitaxeAutotuningApp()
        app.run()
    except KeyboardInterrupt:
        print("\nProgram interrupted and exiting cleanly...")
