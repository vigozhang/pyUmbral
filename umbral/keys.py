import os
import base64

from typing import Union

from nacl.secret import SecretBox
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.backends.openssl.ec import (
    _EllipticCurvePublicKey, _EllipticCurvePrivateKey
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


from umbral.config import default_params
from umbral.point import Point
from umbral.bignum import BigNum, hash_to_bn
from umbral.params import UmbralParameters

from abc import ABC, abstractmethod
 
class AbstractUmbralKeyingMaterial(ABC):

    @abstractmethod
    def _get_keying_material(self):
        pass

    def to_bytes(self, password: bytes=None, _scrypt_cost: int=20):
        """
        Returns an Umbral private key as a urlsafe base64 encoded string with
        optional symmetric encryption via nacl's Salsa20-Poly1305 and Scrypt
        key derivation. If a password is provided, the user must encode it to
        bytes.

        WARNING: RFC7914 recommends that you use a 2^20 cost value for sensitive
        files. It is NOT recommended to change the `_scrypt_cost` value unless
        you know what you are doing.
        """
        umbral_get_keying_material = self._get_keying_material()

        if password:
            salt = os.urandom(16)

            key = Scrypt(
                salt=salt,
                length=SecretBox.KEY_SIZE,
                n=2**_scrypt_cost,
                r=8,
                p=1,
                backend=default_backend()
            ).derive(password)

            umbral_get_keying_material = SecretBox(key).encrypt(umbral_get_keying_material)
            umbral_get_keying_material += salt

        encoded_key = base64.urlsafe_b64encode(umbral_get_keying_material)
        return encoded_key

    @classmethod
    def _decode_get_keying_material(cls, key_data: bytes, 
                                password: bytes=None, _scrypt_cost: int=20):
        key_bytes = base64.urlsafe_b64decode(key_data)

        if password:
            salt = key_bytes[-16:]
            key_bytes = key_bytes[:-16]

            key = Scrypt(
                salt=salt,
                length=SecretBox.KEY_SIZE,
                n=2**_scrypt_cost,
                r=8,
                p=1,
                backend=default_backend()
            ).derive(password)

            key_bytes = SecretBox(key).decrypt(key_bytes)

        return key_bytes

    
class UmbralPrivateKey(AbstractUmbralKeyingMaterial):
    def __init__(self, bn_key: BigNum, params: UmbralParameters=None):
        """
        Initializes an Umbral private key.
        """
        if params is None:
            params = default_params()

        self.params = params
        self.bn_key = bn_key

    @classmethod
    def gen_key(cls, params: UmbralParameters=None):
        """
        Generates a private key and returns it.
        """
        if params is None:
            params = default_params()

        bn_key = BigNum.gen_rand(params.curve)
        return cls(bn_key, params)

    @classmethod
    def from_bytes(cls, key_data: bytes, params: UmbralParameters=None,
                 password: bytes=None, _scrypt_cost: int=20):
        """
        Loads an Umbral private key from a urlsafe base64 encoded string.
        Optionally, if a password is provided it will decrypt the key using
        nacl's Salsa20-Poly1305 and Scrypt key derivation.

        WARNING: RFC7914 recommends that you use a 2^20 cost value for sensitive
        files. Unless you changed this when you called `to_bytes`, you should
        not change it here. It is NOT recommended to change the `_scrypt_cost`
        value unless you know what you're doing.
        """
        if params is None:
            params = default_params()

        key_bytes = cls._decode_get_keying_material(key_data, password, _scrypt_cost)

        bn_key = BigNum.from_bytes(key_bytes, params.curve)
        return cls(bn_key, params)

    def _get_keying_material(self):
        return self.bn_key.to_bytes()

    def get_pubkey(self):
        """
        Calculates and returns the public key of the private key.
        """
        return UmbralPublicKey(self.bn_key * self.params.g)

    def to_cryptography_privkey(self):
        """
        Returns a cryptography.io EllipticCurvePrivateKey from the Umbral key.
        """
        backend = default_backend()

        backend.openssl_assert(self.bn_key.group != backend._ffi.NULL)
        backend.openssl_assert(self.bn_key.bignum != backend._ffi.NULL)

        ec_key = backend._lib.EC_KEY_new()
        backend.openssl_assert(ec_key != backend._ffi.NULL)
        ec_key = backend._ffi.gc(ec_key, backend._lib.EC_KEY_free)

        set_group_result = backend._lib.EC_KEY_set_group(
            ec_key, self.bn_key.group
        )
        backend.openssl_assert(set_group_result == 1)

        set_privkey_result = backend._lib.EC_KEY_set_private_key(
            ec_key, self.bn_key.bignum
        )
        backend.openssl_assert(set_privkey_result == 1)

        # Get public key
        point = backend._lib.EC_POINT_new(self.bn_key.group)
        backend.openssl_assert(point != backend._ffi.NULL)
        point = backend._ffi.gc(point, backend._lib.EC_POINT_free)

        with backend._tmp_bn_ctx() as bn_ctx:
            mult_result = backend._lib.EC_POINT_mul(
                self.bn_key.group, point, self.bn_key.bignum, backend._ffi.NULL,
                backend._ffi.NULL, bn_ctx
            )
            backend.openssl_assert(mult_result == 1)

        set_pubkey_result = backend._lib.EC_KEY_set_public_key(ec_key, point)
        backend.openssl_assert(set_pubkey_result == 1)

        evp_pkey = backend._ec_cdata_to_evp_pkey(ec_key)
        return _EllipticCurvePrivateKey(backend, ec_key, evp_pkey)


class UmbralPublicKey(object):
    def __init__(self, point_key, params: UmbralParameters=None):
        """
        Initializes an Umbral public key.
        """
        if params is None:
            params = default_params()

        self.params = params

        if not isinstance(point_key, Point):
            raise TypeError("point_key can only be a Point.  Don't pass anything else.")

        self.point_key = point_key

    @classmethod
    def from_bytes(cls, key_data: bytes, params: UmbralParameters=None, as_b64=True):
        """
        Loads an Umbral public key from a urlsafe base64 encoded string or bytes.
        """
        if params is None:
            params = default_params()

        if as_b64:
            key_bytes = base64.urlsafe_b64decode(key_data)
        else:
            key_bytes = key_data

        point_key = Point.from_bytes(key_bytes, params.curve)
        return cls(point_key, params)

    def to_bytes(self):
        """
        Returns an Umbral public key as a urlsafe base64 encoded string.
        """
        umbral_pub_key = self.point_key.to_bytes()

        encoded_key = base64.urlsafe_b64encode(umbral_pub_key)
        return encoded_key

    def get_pubkey(self):
        raise NotImplementedError

    def to_cryptography_pubkey(self):
        """
        Returns a cryptography.io EllipticCurvePublicKey from the Umbral key.
        """
        backend = default_backend()

        backend.openssl_assert(self.point_key.group != backend._ffi.NULL)
        backend.openssl_assert(self.point_key.ec_point != backend._ffi.NULL)

        ec_key = backend._lib.EC_KEY_new()
        backend.openssl_assert(ec_key != backend._ffi.NULL)
        ec_key = backend._ffi.gc(ec_key, backend._lib.EC_KEY_free)

        set_group_result = backend._lib.EC_KEY_set_group(
            ec_key, self.point_key.group
        )
        backend.openssl_assert(set_group_result == 1)

        set_pubkey_result = backend._lib.EC_KEY_set_public_key(
            ec_key, self.point_key.ec_point
        )
        backend.openssl_assert(set_pubkey_result == 1)

        evp_pkey = backend._ec_cdata_to_evp_pkey(ec_key)
        return _EllipticCurvePublicKey(backend, ec_key, evp_pkey)

    def __bytes__(self):
        """
        Returns an Umbral Public key as a bytestring.
        """
        return self.point_key.to_bytes()

    def __repr__(self):
        return "{}:{}".format(self.__class__, self.point_key.to_bytes().hex()[:15])

    def __eq__(self, other):
        if type(other) == bytes:
            is_eq = bytes(other) == self
        elif hasattr(other, "point_key"):
            is_eq = self.point_key == other.point_key
        else:
            is_eq = False
        return is_eq


class UmbralKeyingMaterial(AbstractUmbralKeyingMaterial):

    def __init__(self, keying_material: bytes=None):
        """
        Initializes an UmbralKeyingMaterial.
        """
        if keying_material:
            if len(keying_material) < 32:
                raise ValueError("UmbralKeyingMaterial must have size at least 32 bytes.")
            self.keying_material = keying_material
        else:
            self.keying_material = os.urandom(32)

    def _get_keying_material(self):
        return self.keying_material

    def derive_private_key_from_label(self, label: bytes, 
                                    salt: bytes=None, params: UmbralParameters=None):
        """
        Derives an UmbralPrivateKey using a KDF from this instance of 
        UmbralKeyingMaterial, a label, and an optional salt.
        """
        if params is None:
            params = default_params()

        hkdf = HKDF(algorithm=hashes.BLAKE2b(64),
                    length=64,
                    salt=salt,
                    info=b"NuCypherKMS/KeyDerivation/"+label,
                    backend=default_backend()
                    )

        bn_key = hash_to_bn(hkdf.derive(self.keying_material), params)
        return UmbralPrivateKey(bn_key, params)

    @classmethod
    def from_bytes(cls, key_data: bytes, password: bytes=None, _scrypt_cost: int=20):
        """
        Loads an UmbralKeyingMaterial from a urlsafe base64 encoded string.
        Optionally, if a password is provided it will decrypt the key using
        nacl's Salsa20-Poly1305 and Scrypt key derivation.

        WARNING: RFC7914 recommends that you use a 2^20 cost value for sensitive
        files. Unless you changed this when you called `to_bytes`, you should
        not change it here. It is NOT recommended to change the `_scrypt_cost`
        value unless you know what you're doing.
        """

        key_bytes = cls._decode_get_keying_material(key_data, password, _scrypt_cost)
        return cls(key_bytes)
