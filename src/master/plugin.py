class Plugin:
	"""
	Base class of a plugin. Use this when writing new plugins. Provides
	several methods to communicate with the clients.

	Provide at least these methods:
	- name(): String ID of the plugin. It must match the name in the client
	    counterpart.
	"""
	def __init__(self, plugins):
		"""
		Initialize. It needs the plugin storage that is to be used
		there. Registers there.
		"""
		self.__plugins = plugins
		plugins.register_plugin(self.name(), self)

	def unregister(self):
		"""
		Unregister from the plugin storage. Call at most once, it
		makes the plugin unusable afterwards.
		"""
		self.__plugins.unregister_plugin(self.name(), self)

	def client_connected(self, client):
		"""
		Called with each newly connected client. A client class
		is passed. It can be used to store the ID (client.cid())
		or directly communicate with it. Please, don't store
		the client itself.
		"""
		pass

	def client_disconnected(self, client):
		"""
		Counterpart of client_connected. It is called after the
		client has already been made unreachable, so there's no
		point in trying to talk to it.
		"""
		pass


class Plugins:
	"""
	Singleton holding all the active plugins and clients. It
	connects them together.
	"""
	def __init__(self):
		self.__plugins = {}
		self.__clients = {}

	def register_plugin(self, name, plugin):
		"""
		Add a plugin to be used.
		"""
		self.__plugins[name] = plugin

	def unregister_plugin(self, name):
		"""
		Remove a plugin.
		"""
		del self.__plugins[name]

	def register_client(self, client):
		"""
		When a client connects.
		"""
		self.__clients[client.cid()] = client
		for p in self.__plugins.values():
			p.client_connected(client)

	def unregister_client(self, client):
		"""
		When a client disconnects.
		"""
		for p in self.__plugins.values():
			p.client_disconnected(client)
		del self.__clients[client.cid()]
