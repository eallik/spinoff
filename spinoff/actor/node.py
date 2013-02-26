# coding: utf-8
from __future__ import print_function

from cPickle import dumps, PicklingError
from cStringIO import StringIO

from spinoff.actor.events import Events, DeadLetter
from spinoff.actor.exceptions import LookupFailed
from spinoff.actor.guardian import Guardian
from spinoff.actor.ref import Ref
from spinoff.actor.uri import Uri
from spinoff.remoting import Hub, HubWithNoRemoting
from spinoff.remoting.pickler import IncomingMessageUnpickler
from spinoff.util.pattern_matching import ANY
from spinoff.actor.events import RemoteDeadLetter


class Node(object):
    """`Node` is both a singleton instance and a class lookalike, there is thus always available a default global
    `Node` instance but it is possible to create more, non-default, instances of `Node` by simply calling it as if it
    were a class, i.e. using it as a class. This is mainly useful for testing multi-node scenarios without any network
    involved by setting a custom `remoting.Hub` to the `Node`.

    """
    _hub = None

    def __init__(self, nid=None, enable_remoting=False, enable_relay=False, hub_kwargs={}):
        self.nid = nid
        self._uri = Uri(name=None, parent=None, node=nid)
        self.guardian = Guardian(uri=self._uri, node=self)
        self._hub = (
            HubWithNoRemoting() if not enable_remoting else
            Hub(nid, enable_relay, on_node_down=lambda ref, nid: ref << ('_node_down', nid), on_receive=self._on_receive, **hub_kwargs)
        )

    def lookup_str(self, addr):
        return self.lookup(Uri.parse(addr))

    def lookup(self, uri):
        if not uri.node or uri.node == self._uri.node:
            try:
                return self.guardian.lookup_ref(uri)
            except LookupFailed if uri.node else None:
                # for remote lookups that actually try to look up something on the local node, we don't want to raise an
                # exception because the caller might not be statically aware of the localness of the `Uri`, thus we
                # return a mere dead ref:
                return Ref(cell=None, uri=uri, is_local=True)
        elif not uri.root:  # pragma: no cover
            raise TypeError("Node can't look up a relative Uri; did you mean Node.guardian.lookup(%r)?" % (uri,))
        else:
            return Ref(cell=None, uri=uri, is_local=False, node=self)

    def spawn(self, *args, **kwargs):
        return self.guardian.spawn(*args, **kwargs)

    def send_message(self, message, remote_ref):
        self._hub.send_message(remote_ref.uri.node, _Msg(remote_ref, message))

    def watch_node(self, nid, watcher):
        self._hub.watch_node(nid, watcher)

    def unwatch_node(self, nid, watcher):
        self._hub.unwatch_node(nid, watcher)

    def _on_receive(self, sender_nid, msg_bytes):
        local_path, message = IncomingMessageUnpickler(self, StringIO(msg_bytes)).load()
        cell = self.guardian.lookup_cell(Uri.parse(local_path))
        if not cell:
            if ('_watched', ANY) == message:
                watched_ref = Ref(cell=None, is_local=True, uri=Uri.parse(self.nid + local_path))
                _, watcher = message
                watcher << ('terminated', watched_ref)
            elif message in (('terminated', ANY), ('_watched', ANY), ('_unwatched', ANY)):
                pass
            else:
                self._remote_dead_letter(local_path, message, sender_nid)
        else:
            cell.receive(message)

    def _remote_dead_letter(self, path, msg, from_):
        ref = Ref(cell=None, uri=Uri.parse(self.nid + path), is_local=True)
        if not (msg == ('_unwatched', ANY) or msg == ('_watched', ANY)):
            Events.log(RemoteDeadLetter(ref, msg, from_))

    def stop(self):
        if getattr(self, 'guardian', None):
            self.guardian.stop()
            self.guardian = None
        if getattr(self, '_hub', None):
            self._hub.stop()
            self._hub = None

    def __getstate__(self):  # pragma: no cover
        raise PicklingError("Node cannot be serialized")

    def __repr__(self):
        return '<node:%s>' % (self._uri if self._hub else 'local')


class _Msg(object):
    def __init__(self, ref, msg):
        self.ref, self.msg = ref, msg

    def serialize(self):
        return dumps((self.ref.uri.path, self.msg), protocol=2)

    def send_failed(self):
        if not (self.msg == ('_unwatched', ANY) or self.msg == ('_watched', ANY)):
            Events.log(DeadLetter(self.ref, self.msg))

    def __repr__(self):
        return "_Msg(%r, %r)" % (self.ref, self.msg)
