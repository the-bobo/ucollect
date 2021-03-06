/*
    Ucollect - small utility for real-time analysis of network data
    Copyright (C) 2015 CZ.NIC, z.s.p.o. (http://www.nic.cz/)

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License along
    with this program; if not, write to the Free Software Foundation, Inc.,
    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
*/

#include "queue.h"

#include "../../core/context.h"
#include "../../core/mem_pool.h"
#include "../../core/util.h"
#include "../../core/loop.h"

#include <stdbool.h>
#include <unistd.h>
#include <errno.h>
#include <string.h>
#include <time.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>

#define QUEUE_FLUSH_TIME 5000
#define QUEUE_RETRY_TIME 60000

struct queue {
	bool active, timeout_started;
	bool broken;
	bool broken_timeout_id;
	int ipset_pipe;
	pid_t pid;
	size_t timeout_id;
	reload_callback_t reload_callback;
};

struct queue *queue_alloc(struct context *context, reload_callback_t reload_callback) {
	struct queue *result = mem_pool_alloc(context->permanent_pool, sizeof *result);
	*result = (struct queue) {
		.reload_callback = reload_callback
	};
	return result;
}

static void start(struct context *context, struct queue *queue) {
	ulog(LLOG_DEBUG, "Starting ipset subcommand\n");
	sanity(!queue->active, "Trying to start already active queue\n");
	int conn[2];
	sanity(socketpair(AF_UNIX, SOCK_STREAM, 0, conn) != -1, "Couldn't create FWUp socketpair: %s\n", strerror(errno));
	struct loop *loop = context->loop;
	/*
	 * Register the local end. This one will be in the parent process,
	 * therefore it needs to be watched and killed in case the plugin
	 * dies. It will also be auto-closed in the child by loop_fork(),
	 * saving us the bother to close it manually there.
	 */
	loop_plugin_register_fd(context, conn[1], queue);
	pid_t pid = loop_fork(loop);
	if (pid)
		// The parent doesn't need the remote end, no matter if the fork worked or not.
		sanity(close(conn[0]) != -1, "Couldn't close the read end of FWUp pipe: %s\n", strerror(errno));
	sanity(pid != -1, "Couldn't fork the ipset command: %s\n", strerror(errno));
	if (pid) {
		// The parent. Update the queue and be done with it.
		queue->active = true;
		queue->ipset_pipe = conn[1];
		queue->pid = pid;
	} else {
		// The child. Screw the socket into our input and stderr and exec to ipset command.
		if (dup2(conn[0], 0) == -1)
			die("Couldn't attach the socketpair to ipset input: %s\n", strerror(errno));
		if (dup2(conn[0], 1) == -1)
			die("Couldn't attach the socketpair to ipset stdout: %s\n", strerror(errno));
		if (dup2(conn[0], 2) == -1)
			die("Couldn't attach the socketpair to ipset stderr: %s\n", strerror(errno));
		// Get rid of the original.
		close(conn[0]);
		execl("/usr/sbin/ipset", "ipset", "-exist", "restore", (char *)NULL);
		// Still here? The above must have failed :-(
		die("Couldn't exec ipset: %s\n", strerror(errno));
	}
}

static void retry_timeout(struct context *context, void *data, size_t id __attribute__((unused))) {
	struct queue *queue = data;
	sanity(!queue->timeout_started, "Timeout started and retry timeout fired\n");
	ulog(LLOG_WARN, "Trying to re-fill IPsets now\n");
	// Leave the broken state and retry filling the ipsets
	queue->broken = false;
	queue->broken_timeout_id = 0;
	sanity(queue->reload_callback, "The reload callback is NULL\n");
	queue->reload_callback(context);
}

static void lost(struct context *context, struct queue *queue, bool error) {
	if (queue->broken)
		// Already lost, don't do it again.
		return;
	/*
	 * In case we got EOF before and errorenous termination of the command later,
	 * we need not to deactivate, close the pipe and such. But we still
	 * want to mark it as broken, start the retry timeout and re-synchronize.
	 *
	 * If the termination comes sooner than EOF (which is likely, but probably not
	 * guaranteed), then we mark it broken & inactive in one go and will not
	 * enter the routine once again.
	 */
	if (queue->active) {
		// Deactivate
		if (error) {
			ulog(LLOG_WARN, "The ipset command %d died, data may be out of sync\n", queue->pid);
			// Don't close the pipe yet. It may contain error messages we would like to pass to the log
		} else {
			ulog(LLOG_DEBUG, "Closing ipset subcommand\n");
			loop_plugin_unregister_fd(context, queue->ipset_pipe);
			sanity(close(queue->ipset_pipe) == 0, "Error closing the ipset pipe: %s\n", strerror(errno));
		}
		queue->ipset_pipe = 0;
		queue->active = false;
		queue->pid = 0;
		if (queue->timeout_started) {
			queue->timeout_started = false;
			loop_timeout_cancel(context->loop, queue->timeout_id);
		}
	} else if (error)
		ulog(LLOG_WARN, "IPset command considered broken post-morten\n");
	if (error) {
		queue->broken = true;
		queue->broken_timeout_id = loop_timeout_add(context->loop, QUEUE_RETRY_TIME, context, queue, retry_timeout);
	}
}

static void flush_timeout(struct context *context, void *data, size_t id __attribute__((unused))) {
	struct queue *queue = data;
	queue->timeout_started = false;
	queue_flush(context, queue);
}

void enqueue(struct context *context, struct queue *queue, const char *command) {
	if (queue->broken) {
		ulog(LLOG_DEBUG_VERBOSE, "Not queueing command '%s', the queue is currently broken\n", command);
		return;
	}
	if (!queue->active)
		start(context, queue);
	sanity(queue->active, "Failed to start the queue\n");
	sanity(queue->ipset_pipe > 0, "Strange pipe FD to the ip set command: %i\n", queue->ipset_pipe);
	size_t len = strlen(command);
	sanity(len, "Empty ipset command\n");
	sanity(command[len - 1] == '\n', "IPset command '%s' not terminated by a newline\n", command);
	ulog(LLOG_DEBUG_VERBOSE, "IPset command %s", command); // Now newline at the end of format, command contains one
	while (len) {
		ssize_t sent = send(queue->ipset_pipe, command, len, MSG_NOSIGNAL);
		if (sent == -1) {
			switch (errno) {
				case ECONNRESET:
				case EPIPE:
					lost(context, queue, true);
					return;
				case EINTR:
					ulog(LLOG_WARN, "Interrupted while writing data to ipset, retrying\n");
					continue;
				default:
					sanity(false, "Error writing to ipset: %s\n", strerror(errno));
			}
		}
		// Move forward in whatever was sent
		command += sent;
		len -= sent;
	}
	if (!queue->timeout_started) {
		queue->timeout_started = true;
		queue->timeout_id = loop_timeout_add(context->loop, QUEUE_FLUSH_TIME, context, queue, flush_timeout);
	}
}

void queue_flush(struct context *context, struct queue *queue) {
	lost(context, queue, false);
}

void queue_fd_data(struct context *context, int fd, void *userdata) {
	struct queue *q = userdata;
	const size_t buf_size = 512;
	char *err_msg = mem_pool_alloc(context->temp_pool, buf_size + 1 /* Extra one for \0 at the end */);
	ssize_t result = recv(fd, err_msg, buf_size, MSG_DONTWAIT);
	switch (result) {
		case -1:
			switch (errno) {
				case EAGAIN:
#if EAGAIN != EWOULDBLOCK
				case EWOULDBLOCK:
#endif
				case EINTR:
					// It might work next time
					return;
				default:
					// Default isn't last on purpose.
					insane("Error reading from IPSet stderr: %s\n", strerror(errno));
				case ECONNRESET:
					; // Fall through from this case statement out and out of the case -1 into the close
			}
			// No break. See the above comment.
		case 0: // Close
			ulog(LLOG_WARN, "IPSet closed by the other end\n");
			if (q->active)
				lost(context, q, false);
			else {
				// The command terminated, but the current file descriptor was still alive for a whlie. Close it.
				loop_plugin_unregister_fd(context, fd);
				sanity(close(fd) == 0, "Error closing the ipset pipe: %s\n", strerror(errno));
			}
			return;
		default:
			err_msg[result] = '\0';
			if (err_msg[result - 1] == '\n')
				err_msg[result - 1] = '\0';
			char *pos = err_msg;
			while ((pos = index(pos, '\n')))
				*pos = '\\';
			ulog(LLOG_WARN, "IPSet output: %s\n", err_msg);
			return;
	}
}

void queue_child_died(struct context *context, int state, pid_t child, struct queue *queue) {
	if (!queue->active)
		return;		// It can't be our child, no queue is currently active
	if (queue->pid != child)
		return;		// Not our child, something else died
	bool broken = true;
	if (WIFEXITED(state)) {
		int ecode = WEXITSTATUS(state);
		if (ecode != 0)
			ulog(LLOG_ERROR, "The ipset command %d terminated with status %d\n", (int)child, ecode);
		else {
			ulog(LLOG_DEBUG, "The ipset command %d terminated successfully\n", (int)child);
			broken = false;
		}
	} else if (WIFSIGNALED(state)) {
		int signal = WTERMSIG(state);
		ulog(LLOG_ERROR, "The ipset command %d terminated with signal %d\n", (int)child, signal);
	} else
		ulog(LLOG_ERROR, "The ipset command %d died for unknown reason, call the police to investigate\n", (int)child);
	lost(context, queue, broken);
}
