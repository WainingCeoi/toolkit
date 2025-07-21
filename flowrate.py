number_of_points = None

while True:
    try:
        number_of_points = int(input("\nEnter the No. of end points: "))
        match number_of_points:
            case _ if number_of_points == 0:
                break
            case _ if number_of_points < 0:
                print("\nInput must be positive")
            case _:
                flow_rate = (number_of_points)**0.73/23
                print(f"L={flow_rate:.3f} L/s")
    except ValueError:
        print("\nInput must be integer.")