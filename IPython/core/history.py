""" History related magics and functionality """
#-----------------------------------------------------------------------------
#  Copyright (C) 2010 The IPython Development Team.
#
#  Distributed under the terms of the BSD License.
#
#  The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------
from __future__ import print_function

# Stdlib imports
import datetime
import json
import os
import re
import sqlite3

from collections import defaultdict

# Our own packages
from IPython.config.configurable import Configurable
import IPython.utils.io

from IPython.testing import decorators as testdec
from IPython.utils.io import ask_yes_no
from IPython.utils.traitlets import Bool, Dict, Instance, Int, List, Unicode
from IPython.utils.warn import warn

#-----------------------------------------------------------------------------
# Classes and functions
#-----------------------------------------------------------------------------

class HistoryManager(Configurable):
    """A class to organize all history-related functionality in one place.
    """
    # Public interface

    # An instance of the IPython shell we are attached to
    shell = Instance('IPython.core.interactiveshell.InteractiveShellABC')
    # Lists to hold processed and raw history. These start with a blank entry
    # so that we can index them starting from 1
    input_hist_parsed = List([""])
    input_hist_raw = List([""])
    # A list of directories visited during session
    dir_hist = List()
    # A dict of output history, keyed with ints from the shell's execution count
    output_hist = Instance(defaultdict)
    # String holding the path to the history file
    hist_file = Unicode()
    # The SQLite database
    db = Instance(sqlite3.Connection)
    # The number of the current session in the history database
    session_number = Int()
    # Should we log output to the database? (default no)
    db_log_output = Bool(False, config=True)
    # Write to database every x commands (higher values save disk access & power)
    #  Values of 1 or less effectively disable caching. 
    db_cache_size = Int(0, config=True)
    # The input and output caches
    db_input_cache = List()
    db_output_cache = List()
    
    # Private interface
    # Variables used to store the three last inputs from the user.  On each new
    # history update, we populate the user's namespace with these, shifted as
    # necessary.
    _i00, _i, _ii, _iii = '','','',''

    # A set with all forms of the exit command, so that we don't store them in
    # the history (it's annoying to rewind the first entry and land on an exit
    # call).
    _exit_commands = None
    
    def __init__(self, shell, config=None):
        """Create a new history manager associated with a shell instance.
        """
        # We need a pointer back to the shell for various tasks.
        super(HistoryManager, self).__init__(shell=shell, config=config)

        # list of visited directories
        try:
            self.dir_hist = [os.getcwd()]
        except OSError:
            self.dir_hist = []

        # Now the history file
        if shell.profile:
            histfname = 'history-%s' % shell.profile
        else:
            histfname = 'history'
        self.hist_file = os.path.join(shell.ipython_dir, histfname + '.sqlite')
        self.init_db()
        self.new_session()
    
        self._i00, self._i, self._ii, self._iii = '','','',''
        self.output_hist = defaultdict(list)

        self._exit_commands = set(['Quit', 'quit', 'Exit', 'exit', '%Quit',
                                   '%quit', '%Exit', '%exit'])
        
    def init_db(self):
        """Connect to the database, and create tables if necessary."""
        self.db = sqlite3.connect(self.hist_file)
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (session integer
                        primary key autoincrement, start timestamp,
                        end timestamp, num_cmds integer, remark text)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS history 
                (session integer, line integer, source text, source_raw text,
                PRIMARY KEY (session, line))""")
        # Output history is optional, but ensure the table's there so it can be
        # enabled later.
        self.db.execute("""CREATE TABLE IF NOT EXISTS output_history
                        (session integer, line integer, output text,
                        PRIMARY KEY (session, line))""")
        self.db.commit()
    
    def new_session(self):
        """Get a new session number."""
        with self.db:
            cur = self.db.execute("""INSERT INTO sessions VALUES (NULL, ?, NULL,
                            NULL, "") """, (datetime.datetime.now(),))
            self.session_number = cur.lastrowid
            
    def end_session(self):
        """Close the database session, filling in the end time and line count."""
        self.writeout_cache()
        with self.db:
            self.db.execute("""UPDATE sessions SET end=?, num_cmds=? WHERE
                            session==?""", (datetime.datetime.now(),
                            len(self.input_hist_parsed)-1, self.session_number))
        self.session_number = 0
                            
    def name_session(self, name):
        """Give the current session a name in the history database."""
        with self.db:
            self.db.execute("UPDATE sessions SET remark=? WHERE session==?",
                            (name, self.session_number))
                            
    def reset(self, new_session=True):
        """Clear the session history, releasing all object references, and
        optionally open a new session."""
        if self.session_number:
            self.end_session()
        self.input_hist_parsed[:] = [""]
        self.input_hist_raw[:] = [""]
        self.output_hist.clear()
        # The directory history can't be completely empty
        self.dir_hist[:] = [os.getcwd()]
        
        if new_session:
            self.new_session()
    
    ## -------------------------------
    ## Methods for retrieving history:
    ## -------------------------------
    def _get_hist_sql(self, sql, params, raw=True, output=False):
        """Prepares and runs an SQL query for the history database.
        
        Parameters
        ----------
        sql : str
          Any filtering expressions to go after SELECT ... FROM ...
        params : tuple
          Parameters passed to the SQL query (to replace "?")
        raw : bool
          If True, get raw input.
        output : bool
          If True, include output where available.
        
        Returns
        -------
        An iterator over 3-tuples: (session, line_number, command), or if output
        is True, (session, line_number, (command, output)).
        """
        toget = 'source_raw' if raw else 'source'
        sqlfrom = "history"
        if output:
            sqlfrom = "history LEFT JOIN output_history USING (session, line)"
            toget = "history.%s, output_history.output" % toget
        cur = self.db.execute("SELECT session, line, %s FROM %s " %\
                                (toget, sqlfrom) + sql, params)
        if output:    # Regroup into 3-tuples, and parse JSON
            loads = lambda out: json.loads(out) if out else None
            return ((ses, lin, (inp, loads(out))) \
                                        for ses, lin, inp, out in cur)
        return cur
        
    
    def get_hist_tail(self, n=10, raw=True, output=False):
        """Get the last n lines from the history database."""
        self.writeout_cache()
        cur = self._get_hist_sql("ORDER BY session DESC, line DESC LIMIT ?",
                                (n,), raw=raw, output=output)
        return reversed(list(cur))
        
    def get_hist_search(self, pattern="*", raw=True, output=False):
        """Search the database using unix glob-style matching (wildcards * and
        ?, escape using \).
        
        Returns
        -------
        An iterator over tuples: (session, line_number, command)
        """
        tosearch = "source_raw" if raw else "source"
        if output:
            tosearch = "history." + tosearch
        self.writeout_cache()
        return self._get_hist_sql("WHERE %s GLOB ?" % tosearch, (pattern,),
                                    raw=raw, output=output)
                                
    def _get_hist_session(self, start=1, stop=None, raw=True, output=False):
        """Get input and output history from the current session. Called by
        get_history, and takes similar parameters."""
        input_hist = self.input_hist_raw if raw else self.input_hist_parsed
            
        n = len(input_hist)
        if start < 0:
            start += n
        if not stop:
            stop = n
        elif stop < 0:
            stop += n
        
        for i in range(start, stop):
            if output:
                output_item = [repr(x) for x in self.output_hist[i]]
                line = (input_hist[i], output_item)
            else:
                line = input_hist[i]
            yield (0, i, line)
            
    def get_history(self, session=0, start=1, stop=None, raw=True,output=False):
        """Retrieve input by session.
        
        Parameters
        ----------
        session : int
            Session number to retrieve. The current session is 0, and negative
            numbers count back from current session, so -1 is previous session.
        start : int
            First line to retrieve.
        stop : int
            End of line range (excluded from output itself). If None, retrieve
            to the end of the session.
        raw : bool
            If True, return untranslated input
        output : bool
            If True, attempt to include output. This will be 'real' Python
            objects for the current session, or text reprs from previous
            sessions if db_log_output was enabled at the time. Where no output
            is found, None is used.
            
        Returns
        -------
        An iterator over the desired lines. Each line is a 3-tuple, either
        (session, line, input) if output is False, or
        (session, line, (input, output)) if output is True.
        """
        if session == 0 or session==self.session_number:   # Current session
            return self._get_hist_session(start, stop, raw, output)
        if session < 0:
            session += self.session_number
            
        if stop:
            lineclause = "line >= ? AND line < ?"
            params = (session, start, stop)
        else:
            lineclause = "line>=?"
            params = (session, start)
        
        return self._get_hist_sql("WHERE session==? AND %s""" % lineclause,
                                    params, raw=raw, output=output)
        
    def get_hist_from_rangestr(self, rangestr, raw=True, output=False):
        """Get lines of history from a string of ranges, as used by magic
        commands %hist, %save, %macro, etc."""
        for sess, s, e in extract_hist_ranges(rangestr):
            for line in self.get_history(sess, s, e, raw=raw, output=output):
                yield line
    
    ## ----------------------------
    ## Methods for storing history:
    ## ----------------------------
    def store_inputs(self, line_num, source, source_raw=None):
        """Store source and raw input in history and create input cache
        variables _i*.
        
        Parameters
        ----------
        line_num : int
          The prompt number of this input.
        
        source : str
          Python input.

        source_raw : str, optional
          If given, this is the raw input without any IPython transformations
          applied to it.  If not given, ``source`` is used.
        """
        if source_raw is None:
            source_raw = source
            
        # do not store exit/quit commands
        if source_raw.strip() in self._exit_commands:
            return
        
        self.input_hist_parsed.append(source.rstrip())
        self.input_hist_raw.append(source_raw.rstrip())
        
        self.db_input_cache.append((self.session_number, line_num,
                                    source, source_raw))
        # Trigger to flush cache and write to DB.
        if len(self.db_input_cache) >= self.db_cache_size:
            self.writeout_cache()

        # update the auto _i variables
        self._iii = self._ii
        self._ii = self._i
        self._i = self._i00
        self._i00 = source_raw

        # hackish access to user namespace to create _i1,_i2... dynamically
        new_i = '_i%s' % line_num
        to_main = {'_i': self._i,
                   '_ii': self._ii,
                   '_iii': self._iii,
                   new_i : self._i00 }
        self.shell.user_ns.update(to_main)
        
    def store_output(self, line_num):
        if (not self.db_log_output) or not self.output_hist[line_num]:
            return
        output = json.dumps([repr(x) for x in self.output_hist[line_num]])
        db_row = (self.session_number, line_num, output)
        if self.db_cache_size > 1:
            self.db_output_cache.append(db_row)
        else:
          with self.db:
            self.db.execute("INSERT INTO output_history VALUES (?,?,?)", db_row)
        
    def writeout_cache(self):
        #print(self.db_input_cache)
        with self.db:
            self.db.executemany("INSERT INTO history VALUES (?, ?, ?, ?)",
                                self.db_input_cache)
            self.db.executemany("INSERT INTO output_history VALUES (?, ?, ?)",
                                self.db_output_cache)
        self.db_input_cache = []
        self.db_output_cache = []

        
# To match, e.g. ~5/8-~2/3
range_re = re.compile(r"""
((?P<startsess>~?\d+)/)?
(?P<start>\d+)                    # Only the start line num is compulsory
((?P<sep>[\-:])
 ((?P<endsess>~?\d+)/)?
 (?P<end>\d+))?
""", re.VERBOSE)

def extract_hist_ranges(ranges_str):
    """Turn a string of history ranges into 3-tuples of (session, start, stop).
    
    Examples
    --------
    list(extract_input_ranges("~8/5-~7/4 2"))
    [(-8, 5, None), (-7, 1, 4), (0, 2, 3)]
    """
    for range_str in ranges_str.split():
        rmatch = range_re.match(range_str)
        if not rmatch:
            continue
        start = int(rmatch.group("start"))
        end = rmatch.group("end")
        end = int(end) if end else start+1   # If no end specified, get (a, a+1)
        if rmatch.group("sep") == "-":       # 1-3 == 1:4 --> [1, 2, 3]
            end += 1
        startsess = rmatch.group("startsess") or "0"
        endsess = rmatch.group("endsess") or startsess
        startsess = int(startsess.replace("~","-"))
        endsess = int(endsess.replace("~","-"))
        assert endsess >= startsess

        if endsess == startsess:
            yield (startsess, start, end)
            continue
        # Multiple sessions in one range:
        yield (startsess, start, None)
        for sess in range(startsess+1, endsess):
            yield (sess, 1, None)
        yield (endsess, 1, end)

def _format_lineno(session, line):
    """Helper function to format line numbers properly."""
    if session == 0:
        return str(line)
    return "%s#%s" % (session, line)

@testdec.skip_doctest
def magic_history(self, parameter_s = ''):
    """Print input history (_i<n> variables), with most recent last.
    
    %history       -> print at most 40 inputs (some may be multi-line)\\
    %history n     -> print at most n inputs\\
    %history n1 n2 -> print inputs between n1 and n2 (n2 not included)\\

    By default, input history is printed without line numbers so it can be
    directly pasted into an editor.

    With -n, each input's number <n> is shown, and is accessible as the
    automatically generated variable _i<n> as well as In[<n>].  Multi-line
    statements are printed starting at a new line for easy copy/paste.

    Options:

      -n: print line numbers for each input.
      This feature is only available if numbered prompts are in use.

      -o: also print outputs for each input.

      -p: print classic '>>>' python prompts before each input.  This is useful
       for making documentation, and in conjunction with -o, for producing
       doctest-ready output.

      -r: (default) print the 'raw' history, i.e. the actual commands you typed.
      
      -t: print the 'translated' history, as IPython understands it.  IPython
      filters your input and converts it all into valid Python source before
      executing it (things like magics or aliases are turned into function
      calls, for example). With this option, you'll see the native history
      instead of the user-entered version: '%cd /' will be seen as
      'get_ipython().magic("%cd /")' instead of '%cd /'.
      
      -g: treat the arg as a pattern to grep for in (full) history.
      This includes the saved history (almost all commands ever written).
      Use '%hist -g' to show full saved history (may be very long).
      
      -l: get the last n lines from all sessions. Specify n as a single arg, or
      the default is the last 10 lines.

      -f FILENAME: instead of printing the output to the screen, redirect it to
       the given file.  The file is always overwritten, though IPython asks for
       confirmation first if it already exists.
       
    Examples
    --------
    ::
    
      In [6]: %hist -n 4 6
      4:a = 12
      5:print a**2

    """

    if not self.shell.displayhook.do_full_cache:
        print('This feature is only available if numbered prompts are in use.')
        return
    opts,args = self.parse_options(parameter_s,'noprtglf:',mode='string')
    
    # For brevity
    history_manager = self.shell.history_manager
    
    def _format_lineno(session, line):
        """Helper function to format line numbers properly."""
        if session in (0, history_manager.session_number):
            return str(line)
        return "%s/%s" % (session, line)

    # Check if output to specific file was requested.
    try:
        outfname = opts['f']
    except KeyError:
        outfile = IPython.utils.io.Term.cout  # default
        # We don't want to close stdout at the end!
        close_at_end = False
    else:
        if os.path.exists(outfname):
            if not ask_yes_no("File %r exists. Overwrite?" % outfname): 
                print('Aborting.')
                return

        outfile = open(outfname,'w')
        close_at_end = True
    
    print_nums = 'n' in opts
    get_output = 'o' in opts
    pyprompts = 'p' in opts
    # Raw history is the default
    raw = not('t' in opts)
            
    default_length = 40
    pattern = None
    
    if 'g' in opts:         # Glob search
        pattern = "*" + args + "*" if args else "*"
        hist = history_manager.get_hist_search(pattern, raw=raw,
                                                              output=get_output)
    elif 'l' in opts:       # Get 'tail'
        try:
            n = int(args)
        except ValueError, IndexError:
            n = 10
        hist = history_manager.get_hist_tail(n, raw=raw, output=get_output)
    else:
        if args:            # Get history by ranges
            hist = history_manager.get_hist_from_rangestr(args, raw, get_output)
        else:               # Just get history for the current session
            hist = history_manager.get_history(raw=raw, output=get_output)
    
    # We could be displaying the entire history, so let's not try to pull it 
    # into a list in memory. Anything that needs more space will just misalign.
    width = 4
        
    for session, lineno, inline in hist:
        # Print user history with tabs expanded to 4 spaces.  The GUI clients
        # use hard tabs for easier usability in auto-indented code, but we want
        # to produce PEP-8 compliant history for safe pasting into an editor.
        if get_output:
            inline, output = inline
        inline = inline.expandtabs(4).rstrip()
            
        multiline = "\n" in inline
        line_sep = '\n' if multiline else ' '
        if print_nums:
            print('%s:%s' % (_format_lineno(session, lineno).rjust(width),
                    line_sep),  file=outfile, end='')
        if pyprompts:
            print(">>> ", end="", file=outfile)
            if multiline:
                inline = "\n... ".join(inline.splitlines()) + "\n..."
        print(inline, file=outfile)
        if get_output and output:
            print("\n".join(output), file=outfile)

    if close_at_end:
        outfile.close()


def magic_rep(self, arg):
    r""" Repeat a command, or get command to input line for editing

    - %rep (no arguments):
    
    Place a string version of last computation result (stored in the special '_'
    variable) to the next input prompt. Allows you to create elaborate command
    lines without using copy-paste::
    
        $ l = ["hei", "vaan"]       
        $ "".join(l)        
        ==> heivaan        
        $ %rep        
        $ heivaan_ <== cursor blinking    
    
    %rep 45
    
    Place history line 45 to next input prompt. Use %hist to find out the
    number.
    
    %rep 1-4 6-7 3
    
    Repeat the specified lines immediately. Input slice syntax is the same as
    in %macro and %save.
    
    %rep foo
    
    Place the most recent line that has the substring "foo" to next input.
    (e.g. 'svn ci -m foobar').    
    """
    
    opts,args = self.parse_options(arg,'',mode='list')
    if not args:                # Last output
        self.set_next_input(str(self.shell.user_ns["_"]))
        return

    arg = " ".join(args)
    histlines = self.history_manager.get_hist_from_rangestr(arg, raw=False)
    histlines = [x[2] for x in histlines]
    
    if len(histlines) > 1:      # Execute immediately
        histlines = "\n".join(histlines)
        print("=== Executing: ===")
        print(histlines)
        print("=== Output: ===")
        self.run_source(histlines, symbol="exec")
    
    elif len(histlines) == 1:   # Editable input
        self.set_next_input(histlines[0].rstrip())
    
    else:                       # Search for term - editable input
        histlines = self.history_manager.get_hist_search("*"+arg+"*")
        for h in reversed([x[2] for x in histlines]):
            if 'rep' in h:
                continue
            self.set_next_input(h.rstrip())
            return
        print("Not found in history:", arg)


def init_ipython(ip):
    ip.define_magic("rep", magic_rep) 
    ip.define_magic("hist",magic_history)    # Alternative name
    ip.define_magic("history",magic_history)

    # XXX - ipy_completers are in quarantine, need to be updated to new apis
    #import ipy_completers
    #ipy_completers.quick_completer('%hist' ,'-g -t -r -n')
