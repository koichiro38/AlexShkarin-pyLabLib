from ..core.thread import controller, multicast_pool
from ..core.utils import functions

from ..core.gui import QtCore, Slot, Signal

import collections



class ScriptStopException(Exception):
    """Exception for stopping script execution"""

TMulticastWaitResult=collections.namedtuple("TMulticastWaitResult",["monitor","message"])
class ScriptThread(controller.QTaskThread):
    """
    A script thread.
    
    Designed to provide means of writing code which interacts with multiple device threads,
    but reads similar to a standard single-threaded script.
    To do that, it provides a mechanism of multicast monitors: one can suspend execution until a multicast with certain properties has been received.
    This can be used to implement, e.g., waiting until the next stream_format/daq sample or a next camera frame.

    Args:
        name (str): thread name
        args: args supplied to :meth:`setup_script` method
        kwargs: keyword args supplied to :meth:`setup_script` method
        multicast_pool: :class:`.MulticastPool` for this thread (by default, use the default common pool)

    Attributes:
        executing (bool): shows whether the script is executing right now;
            useful in :meth:`interrupt_script` to check whether it is while the script is running and is done / stopped by user / terminated (then it would be ``True``),
            or if the script was waiting to be executed / done executing (then it would be ``False``)
            Duplicates ``interrupt_reason`` attribute (``executing==False`` if and only if ``interrupt_reason=="shutdown"``)
        stop_request (bool): shows whether stop has been requested from another thread (by calling :meth:`stop_execution`).
        interrupt_reason (str): shows the reason for calling :meth:`interrupt_script`;
            can be ``"done"`` (called in the end of regularly executed script), ``"stopped"`` (called if the script is forcibly stopped),
            ``"failed"`` (called if the thread is shut down while the script is active,
            e.g., due to error in the script or any other thread, or if the application is closing),
            or ``"shutdown"`` (called when the script is shut down while being inactive)

    Methods to overload:
        - :meth:`setup_script`: executed on the thread startup (between synchronization points ``"start"`` and ``"run"``)
        - :meth:`finalize_script`: executed on thread cleanup (attempts to execute in any case, including exceptions); by default, call :meth:`interrupt_script`
        - :meth:`run_script`: execute the script (can be run several times per script lifetime)
        - :meth:`interrupt_script`: executed when the script is finished or forcibly shut down (including due to exception or application shutdown)
    """
    def __init__(self, name=None, args=None, kwargs=None, multicast_pool=None):
        controller.QTaskThread.__init__(self,name=name,args=args,kwargs=kwargs,multicast_pool=multicast_pool)
        self._monitor_signal.connect(self._on_monitor_signal,QtCore.Qt.QueuedConnection)
        self._monitored_signals={}
        self.executing=False
        self.interrupt_reason="shutdown"
        self.stop_request=False
        self.add_command("start_script",self._start_script)

    def process_interrupt(self, tag, value):
        if controller.QTaskThread.process_interrupt(self,tag,value):
            return True
        if tag=="control.start":
            self.ca.start_script()
            if self.executing:
                self.stop_request=True
            return True
        if tag=="control.stop":
            self.stop_request=True
            return True
        return False

    def setup_script(self, *args, **kwargs):
        """Setup script thread (to be overloaded in subclasses)"""
    def finalize_script(self):
        """
        Finalize script thread (to be overloaded in subclasses)
        
        By default, calls :meth:`interrupt_script`.
        """
        self.interrupt_script()
    def run_script(self):
        """Execute script (to be overloaded in subclasses)"""
    def interrupt_script(self, kind="default"):
        """Finalize script execution (the thread is still running, i.e., the script might be started again)"""
    def check_stop(self, check_messages=True):
        """
        Check if the script stop is requested.

        If it is, raise :exc:`ScriptStopException` which effectively stops execution past this point
        (the exception is properly caught and processed elsewhere in the service code).
        To only check if the stop has been requested without exception raising, use ``stop_request`` attribute.
        If ``check_messages==True``, check for new messages from other threads first.
        """
        if check_messages:
            self.check_messages()
        if self.stop_request:
            self.stop_request=False
            raise ScriptStopException()



    def setup_task(self, *args, **kwargs):
        functions.call_cut_args(self.setup_script,*args,**kwargs)
    def finalize_task(self):
        self.finalize_script()

    def _start_script(self):
        self.executing=True
        self.stop_request=False
        try:
            self.interrupt_reason="done"
            self.run_script()
            self.interrupt_script()
            self.interrupt_reason="shutdown"
            self.executing=False
        except ScriptStopException:
            self.interrupt_reason="stopped"
            self.interrupt_script()
            self.interrupt_reason="shutdown"
            self.executing=False
        except:
            self.interrupt_reason="failed"
            raise

    _monitor_signal=Signal(object)
    @Slot(object)
    def _on_monitor_signal(self, value):
        mon,msg=value
        try:
            signal=self._monitored_signals[mon]
            if not signal.paused:
                signal.messages.append(msg)
        except KeyError:
            pass
    
    class MonitoredSignal(object): # TODO: signal -> multicast; put in separate class?
        def __init__(self, uid):
            object.__init__(self)
            self.uid=uid
            self.messages=[]
            self.paused=True
    def add_signal_monitor(self, mon, srcs="any", dsts="any", tags=None, filt=None):
        """
        Add a new signal monitor.

        The monitoring isn't started until :meth:`start_monitoring` is called.
        `mon` specifies monitor name; the rest of the arguments are the same as :meth:`.MulticastPool.subscribe`
        """
        if mon in self._monitored_signals:
            raise KeyError("signal monitor {} already exists".format(mon))
        uid=self.subscribe_nonsync(lambda *msg: self._monitor_signal.emit((mon,multicast_pool.TMulticast(*msg))),srcs=srcs,tags=tags,dsts=dsts,filt=filt)
        self._monitored_signals[mon]=self.MonitoredSignal(uid)
    def remove_signal_monitor(self, mon):
        """Remove signal monitor with a given name"""
        if mon not in self._monitored_signals:
            raise KeyError("signal monitor {} doesn't exist".format(mon))
        uid,_=self._monitored_signals.pop(mon)
        self.unsubscribe(uid)
    def wait_for_signal_monitor(self, mons, timeout=None):
        """
        Wait for a signal to be received on a given monitor or several monitors 
        
        If several monitors are given (`mon` is a list), wait for a signal on any of them.
        After waiting is done, pop and return signal value (see :meth:`pop_monitored_signal`).
        """
        if not isinstance(mons,(list,tuple)):
            mons=[mons]
        for mon in mons:
            if mon not in self._monitored_signals:
                raise KeyError("signal monitor {} doesn't exist".format(mon))
        def check_monitors(pop=False):
            for mon in mons:
                if self._monitored_signals[mon].messages:
                    return TMulticastWaitResult(mon,self._monitored_signals[mon].messages.pop(0)) if pop else True
        result=check_monitors(pop=True)
        if result is not None:
            return result
        self.wait_until(check_monitors,timeout=timeout)
        return check_monitors(pop=True)
    def new_monitored_signals_number(self, mon):
        """Get number of received signals at a given monitor"""
        if mon not in self._monitored_signals:
            raise KeyError("signal monitor {} doesn't exist".format(mon))
        return len(self._monitored_signals[mon].messages)
    def pop_monitored_signal(self, mon, n=None):
        """
        Pop data from the given signal monitor queue.

        `n` specifies number of signals to pop (by default, only one).
        Each signal is a tuple ``(mon, sig)`` of monitor name and signal,
        where `sig` is in turn tuple ``(src, tag, value)`` describing the signal.
        """
        if self.new_monitored_signals_number(mon):
            if n is None:
                return self._monitored_signals[mon].messages.pop(0)
            else:
                return [self._monitored_signals[mon].messages.pop(0) for _ in range(n)]
        return None
    def reset_monitored_signal(self, mon):
        """Reset monitored signal (clean its received signals queue)"""
        self._monitored_signals[mon].messages.clear()
    def pause_monitoring(self, mon, paused=True):
        """Pause or un-pause signal monitoring"""
        self._monitored_signals[mon].paused=paused
    def start_monitoring(self, mon):
        """Start signal monitoring"""
        self.pause_monitoring(mon,paused=False)


    def start_execution(self):
        """Request starting script execution"""
        self.send_interrupt("control.start",None)
    def stop_execution(self):
        """Request stopping script execution"""
        self.send_interrupt("control.stop",None)