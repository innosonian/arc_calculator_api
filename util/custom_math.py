"""
The existing round() function has an issue.
round(1.5) = 2 and round(2.5) = 2, which is not accurate.
Therefore, a custom function is created to perform accurate rounding.
"""
def custom_round(number):
    return int(number + 0.5)