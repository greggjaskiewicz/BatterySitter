import aiohttp
import logging
import time
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import base64
import re

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

logger = logging.getLogger(__name__)

REGION_BASE_URLS = {
    'eu': "https://api-eu.sigencloud.com/",
    'cn': "https://api-cn.sigencloud.com/",
    'apac': "https://api-apac.sigencloud.com/",
    'us': "https://api-us.sigencloud.com/"
}

is_dev_test = True
batch_key = '8c7fa517c8442b1a' if is_dev_test else 'xo6as8fjnq3kljfo'


async def create_dynamic_methods(sigen):
    await sigen.get_operational_modes()
    operational_modes = sigen.operational_modes

    for mode in operational_modes:
        method_name = f"set_operational_mode_{mode['label'].lower().replace(' ', '_').replace('-', '_')}"
        mode_value = int(mode['value'])

        async def method(self, value=mode_value):
            await self.set_operational_mode(value)

        method.__name__ = method_name
        setattr(Sigen, method_name, method)


class Sigen:

    def __init__(self, username: str, password: str, region: str = 'eu'):
        self.ac_ev_max_dlm_status = None
        self.ac_ev_max_current = None
        self.ac_ev_last_set_current = None
        self.username = username
        self.password = encrypt_password(password)
        self.token_info = None
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = None
        self.headers = None
        self.station_id = None
        self.ac_sn = None
        self.dc_sn = None
        self.operational_modes = None

        if region not in REGION_BASE_URLS:
            raise ValueError(f"Unsupported region '{region}'. Supported regions are: {', '.join(REGION_BASE_URLS.keys())}")
        self.BASE_URL = REGION_BASE_URLS[region]

    async def async_initialize(self):
        await self.get_access_token()
        await self.fetch_station_info()
        await create_dynamic_methods(self)

    async def get_access_token(self):
        url = f"{self.BASE_URL}auth/oauth/token"
        data = {
            'username': self.username,
            'password': self.password,
            'grant_type': 'password'
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, auth=aiohttp.BasicAuth('sigen', 'sigen')) as response:
                if response.status == 401:
                    raise Exception(
                        f"\n\nPOST {url}\n\nFailed to get access token for user '{self.username}'\nResponse code: {response.status} \nResponse text: '{await response.text()}'\nCheck basic auth is working.")
                if response.status == 200:
                    response_json = await response.json()
                    if 'data' not in response_json:
                        raise Exception(
                            f"\n\nPOST {url}\n\nFailed to get access token for user '{self.username}'\nResponse text: '{await response.text()}'")
                    response_data = response_json['data']
                    if response_data is None or 'access_token' not in response_data or 'refresh_token' not in response_data or 'expires_in' not in response_data:
                        raise Exception(
                            f"\n\nPOST {url}\n\nFailed to get access token for user '{self.username}'\nResponse text: '{await response.text()}'")
                    self.token_info = response_data
                    self.access_token = self.token_info['access_token']
                    self.refresh_token = self.token_info['refresh_token']
                    self.token_expiry = time.time() + self.token_info['expires_in']
                    self.headers = {
                        'Authorization': f'Bearer {self.access_token}',
                        'Content-Type': 'application/json'
                    }
                else:
                    raise Exception(
                        f"\n\nPOST {url}\n\nFailed to get access token for user '{self.username}'\nResponse code: {response.status} \nResponse text: '{await response.text()}'")

    async def refresh_access_token(self):
        url = f"{self.BASE_URL}auth/oauth/token"
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, auth=aiohttp.BasicAuth('sigen', 'sigen')) as response:
                if response.status == 200:
                    response_json = await response.json()
                    response_data = response_json['data']
                    if response_data and 'access_token' in response_data and 'refresh_token' in response_data and 'expires_in' in response_data:
                        self.access_token = response_data['access_token']
                        self.refresh_token = response_data['refresh_token']
                        self.token_expiry = time.time() + response_data['expires_in']
                        self.headers['Authorization'] = f'Bearer {self.access_token}'
                    else:
                        raise Exception(
                            f"\n\nPOST {url}\n\nFailed to refresh access token\nResponse text: '{await response.text()}'")
                else:
                    raise Exception(
                        f"\n\nPOST {url}\n\nFailed to refresh access token\nResponse code: {response.status} \nResponse text: '{await response.text()}'")

    async def ensure_valid_token(self):
        if time.time() >= self.token_expiry:
            await self.refresh_access_token()

    async def fetch_station_info(self):
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/owner/station/home"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                data = (await response.json())['data']
                self.station_id = data['stationId']

                if data['hasAcCharger']:
                    # safely get first element of list data['acSnList']
                    self.ac_sn = data['acSnList'][0] if data['acSnList'] else None

                self.dc_sn = data['dcSnList'][0] if data['dcSnList'] else None

                logger.debug(f"Station ID: {self.station_id}")
                logger.debug(f"Has PV: {data['hasPv']}")
                logger.debug(f"Has EV: {data['hasEv']}")
                logger.debug(f"hasAcCharger: {data['hasAcCharger']}")
                logger.debug(f"acSnList: {data['acSnList']}")
                logger.debug(f"dcSnList: {data['dcSnList']}")
                logger.debug(f"On Grid: {data['onGrid']}")
                logger.debug(f"PV Capacity: {data['pvCapacity']} kW")
                logger.debug(f"Battery Capacity: {data['batteryCapacity']} kWh")

                return data

    async def get_energy_flow(self):
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/sigen/station/energyflow?id={self.station_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                return (await response.json())['data']

    async def get_operational_mode(self):
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/setting/operational/mode/{self.station_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                current_mode = (await response.json())['data']

                if self.operational_modes is None:
                    await self.fetch_operational_modes()

                for mode in self.operational_modes:
                    if mode['value'] == str(current_mode):
                        return mode['label']

                return "Unknown mode"

    async def fetch_operational_modes(self):
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/sigen/station/operational/mode/v/{self.station_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                self.operational_modes = (await response.json())['data']

    async def set_operational_mode(self, mode: int):
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/setting/operational/mode/"
        payload = {
            'stationId': self.station_id,
            'operationMode': mode
        }
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=self.headers, json=payload) as response:
                return await response.json()

    async def get_operational_modes(self):
        if not self.operational_modes:
            await self.get_operational_mode()
        return self.operational_modes

    async def set_ac_ev_current(self, amps: int):
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/acevse/charge/current"
        params = {
            'stationId': self.station_id,
            'current': amps
        }
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=self.headers, params=params) as response:
                return await response.json()

    async def get_ac_ev_current(self):
        """
        Get Current Info for AC EVSE. E.g.

        :return:
{
    "code": 0,
    "msg": "success",
    "data": {
        "lastSetCurrent": 30.0,
        "maxCurrent": 30.0,
        "dlmStatus": 1
    }
}
        """
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/acevse/charge/read/current"
        params = {
            'stationId': self.station_id,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params) as response:
                json_response = await response.json()
                self.ac_ev_last_set_current = json_response['data']['lastSetCurrent']
                self.ac_ev_max_current = json_response['data']['maxCurrent']
                self.ac_ev_max_dlm_status = json_response['data']['dlmStatus']
                return json_response

    async def set_ac_ev_dlm_status(self, new_status: int):
        """
        Set DLM Status for AC EVSE.
        :param new_status: 0 (off) or 1 (on)
        :return:
        """
        # check if 1 or 0 entered for new_status
        if new_status not in [0, 1]:
            raise ValueError("DLM new_status must be 0 or 1")

        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/acevse/more-setting"
        payload = {
            'chargingOutputCurrent': None,
            'outputMode': None,
            'stationId': self.station_id,
            'dlmStatus': new_status,
            'meterPhase': None,
            'homeCircuitBreaker': None,
            'phaseAutoSwitch': None,
            'offGridCharge': None,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as response:
                return await response.json()

    async def get_ac_ev_charge_mode(self):
        """
        Get Charge Mode for AC EVSE. E.g.
        :return:
{
"code": 0,
"msg": "success",
"data": {
    "chargeMode": 0,
    "minKeepChargeTime": 5,
    "maxGridChargePower": 7.0,
    "pvEnergyStartPower": 0.0
}
}
        """
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/acevse/charge/mode"
        params = {
            'stationId': self.station_id,
            'snCode': self.ac_sn,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params) as response:
                json_response = await response.json()  # Get the full JSON response
                return json_response['data']

    async def get_signals(self):
        await self.ensure_valid_token()
        url = f"{self.BASE_URL}device/sigen/device/crypto/read/batch"
        get_signals_decrypted_payload_template = "\"snCode\":\"2024052302935\",\"addr\":null,\"modeVersion\":null,\"signalIds\":[2008,2009,2929,2930,2941,2931],\"stationSnCode\":\"2024052302935\"}"
        get_signals_decrypted_payload = re.sub(r'"snCode":"\d+"', f'"snCode":"{self.station_id}"', get_signals_decrypted_payload_template)
        get_signals_decrypted_payload = re.sub(r'"stationSnCode":"\d+"', f'"stationSnCode":"{self.station_id}"', get_signals_decrypted_payload)

        encrypted_payload = encrypt_batch_payload(get_signals_decrypted_payload)

        payload = {
            "encryption": encrypted_payload
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as response:
                if response.status == 200:
                    response_json = await response.json()
                    return decrypt_batch_payload(response_json.get("encryption", {}))
                else:
                    raise Exception(f"Failed to get signals. Response code: {response.status}, Response: {await response.text()}")

def encrypt_password(password):
    key = "sigensigensigenp"
    iv = "sigensigensigenp"

    cipher = AES.new(key.encode('utf-8'), AES.MODE_CBC, iv.encode('latin1'))
    encrypted = cipher.encrypt(pad(password.encode('utf-8'), AES.block_size))
    return base64.b64encode(encrypted).decode('utf-8')

def encrypt_batch_payload(plain_text):
    # Ensure the key length is 16 bytes long
    key = batch_key.encode('utf-8')
    key = pad(key, 16)[:16]

    iv = b'\xe4\xf5\xc4>\x17%\x18\r\xa2{\x03\xed\xf5\n\xaf\xa7'

    # Create AES cipher instance
    cipher = AES.new(key, AES.MODE_CBC, iv)

    # Pad the plain text
    plain_bytes = pad(plain_text.encode('utf-8'), AES.block_size)

    # Encrypt the data
    encrypted_bytes = cipher.encrypt(plain_bytes)

    # Combine IV and encrypted data
    encrypted_data = iv + encrypted_bytes

    # Encode the encrypted data with base64
    encrypted_data_base64 = base64.b64encode(encrypted_data)

    return encrypted_data_base64.decode('utf-8')

def decrypt_batch_payload(encrypted_data):
    # Ensure the key length is 16, 24, or 32 bytes long
    key = batch_key.encode('utf-8')
    key = pad(key, 16)[:16]

    # Decode the base64 encoded data
    encrypted_data = base64.b64decode(encrypted_data)

    # Extract the IV from the beginning
    iv = encrypted_data[:AES.block_size]

    # Extract the encrypted data
    encrypted_bytes = encrypted_data[AES.block_size:]

    # Create AES cipher instance
    cipher = AES.new(key, AES.MODE_CBC, iv)

    # Decrypt and unpad the data
    decrypted_bytes = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
    return decrypted_bytes.decode('utf-8')


# Example usage:
# import asyncio
# sigen = Sigen(username="your_username", password="your_password", region="us")
# asyncio.run(sigen.async_initialize())
# asyncio.run(sigen.fetch_station_info())
# print(asyncio.run(sigen.get_energy_flow()))
# print(asyncio.run(sigen.get_operational_mode()))
# print(asyncio.run(sigen.set_operational_mode_sigen_ai_mode()))
# print(asyncio.run(sigen.set_operational_mode_maximum_self_powered()))
# print(asyncio.run(sigen.set_operational_mode_tou()))
# print(asyncio.run(sigen.set_operational_mode_fully_fed_to_grid()))