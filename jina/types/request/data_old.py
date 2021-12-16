import copy
from typing import Optional, Dict, TypeVar, Union

from google.protobuf import json_format

from docarray.proto.docarray_pb2 import DocumentArrayProto
from . import Request
from docarray.simple import StructView
from ... import DocumentArray
from ...excepts import BadRequestType
from ...helper import typename, random_identity
from ...proto import jina_pb2

RequestSourceType = TypeVar(
    'RequestSourceType', jina_pb2.DataRequestProto, str, Dict, bytes
)


class DataRequestOld(Request):
    """
    :class:`DataRequest` is one of the **primitive data type** in Jina.

    It offers a Pythonic interface to allow users access and manipulate
    :class:`jina.jina_pb2.DataRequestProto` object without working with Protobuf itself.

    A container for serialized :class:`jina_pb2.DataRequestProto` that only triggers deserialization
    and decompression when receives the first read access to its member.

    It overrides :meth:`__getattr__` to provide the same get/set interface as an
    :class:`jina_pb2.DataRequestProto` object.

    :param request: The request.
    """

    def __init__(
        self,
        request: Optional[RequestSourceType] = None,
    ):
        self._docs = None
        try:
            if isinstance(request, jina_pb2.DataRequestProtoOld):
                self._pb_body = request
            elif isinstance(request, dict):
                self._pb_body = jina_pb2.DataRequestProtoOld()
                json_format.ParseDict(request, self._pb_body)
            elif isinstance(request, str):
                self._pb_body = jina_pb2.DataRequestProtoOld()
                json_format.Parse(request, self._pb_body)
            elif isinstance(request, bytes):
                r = jina_pb2.DataRequestProtoOld()
                r.ParseFromString(request)
                self._pb_body = r
            elif request is not None:
                # note ``None`` is not considered as a bad type
                raise ValueError(f'{typename(request)} is not recognizable')
            else:
                self._pb_body = jina_pb2.DataRequestProtoOld()
        except Exception as ex:
            raise BadRequestType(
                f'fail to construct a {self.__class__} object from {request}'
            ) from ex

    @property
    def proto(self) -> 'jina_pb2.DataRequestProtoOld':
        """
        Cast ``self`` to a :class:`jina_pb2.DataRequestProto`. Laziness will be broken and serialization will be recomputed when calling
        :meth:`SerializeToString`.
        :return: protobuf instance
        """
        return self._pb_body

    @property
    def is_decompressed(self):
        return True

    @property
    def docs(self) -> 'DocumentArray':
        """Get the :class: `DocumentArray` with sequence `data.docs` as content.

        .. # noqa: DAR201"""
        return DocumentArray(self.proto.docs)

    @property
    def parameters(self) -> StructView:
        """Return the `parameters` field of this DataRequest as a Python dict
        :return: a Python dict view of the parameters.
        """
        # if u get this u need to have it decompressed
        return StructView(self.proto.parameters)

    @parameters.setter
    def parameters(self, value: Dict):
        """Set the `parameters` field of this Request to a Python dict
        :param value: a Python dict
        """
        self.proto.parameters.Clear()
        self.proto.parameters.update(value)

    @property
    def response(self):
        """
        Returns the response of this request.

        :return: the response of this request (self) as an instance of Response
        """
        return Response(request=self.proto)

    @property
    def status(self):
        """
        Returns the status from the header field

        :return: the status object of this request
        """
        return self.proto.header.status

    @classmethod
    def from_proto(cls, request: 'jina_pb2.DataRequestProtoOld'):
        """Creates a new DataRequest object from a given :class:`DataRequestProtoOld` object.
        :param request: the to-be-copied data request
        :return: the new message object
        """
        return cls(request=request)

    def __copy__(self, _):
        return DataRequestOld(request=self.proto)

    def __deepcopy__(self, _):
        return DataRequestOld(request=copy.deepcopy(self.proto))


class Response(DataRequestOld):
    """
    Response is the :class:`Request` object returns from the flow. Right now it shares the same representation as
    :class:`Request`. At 0.8.12, :class:`Response` is a simple alias. But it does give a more consistent semantic on
    the client API: send a :class:`Request` and receive a :class:`Response`.

    .. note::
        For now it only exposes `Docs` and `GroundTruth`. Users should very rarely access `Control` commands, so preferably
        not confuse the user by adding `CommandMixin`.
    """