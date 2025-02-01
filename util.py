import phonenumbers
from phonenumbers.phonenumberutil import region_code_for_country_code

def get_country_from_phone_number(phone_number):
    try:
        # Parse the phone number (default region is optional, use "None" if unknown)
        parsed_number = phonenumbers.parse(phone_number, None)
        
        # Extract the country code
        country_code = parsed_number.country_code
        
        # Get the region/country from the country code
        country = region_code_for_country_code(country_code)
        
        return country, country_code
    except phonenumbers.NumberParseException as e:
        print(f"Error parsing phone number: {e}")
        return None, None

def has_country_code(phone_number):
    try:
        # Attempt to parse the phone number
        parsed_number = phonenumbers.parse(phone_number, None)
        
        # Check if the number has a valid country code
        country_code = parsed_number.country_code
        if country_code:
            return True, country_code
    except phonenumbers.NumberParseException:
        pass  # If parsing fails, the number doesn't have a valid country code

    return False, None