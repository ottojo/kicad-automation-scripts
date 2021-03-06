import os
import shutil
import tempfile
import logging
import subprocess
import re
import pytest
from glob import glob
from pty import openpty
from contextlib import contextmanager
from psutil import pid_exists
import sys
# Look for the 'kicad_auto' module from where the script is running
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(script_dir)))
from kicad_auto.ui_automation import recorded_xvfb, PopenContext

COVERAGE_SCRIPT = 'python3-coverage'
KICAD_PCB_EXT = '.kicad_pcb'
KICAD_SCH_EXT = '.sch'
REF_DIR = 'tests/reference'

MODE_SCH = 1
MODE_PCB = 0


class TestContext(object):

    def __init__(self, test_name, prj_name):
        # We are using PCBs
        self.mode = MODE_PCB
        # The name used for the test output dirs and other logging
        self.test_name = test_name
        # The name of the PCB board file
        self.prj_name = prj_name
        # The actual board file that will be loaded
        self._get_board_name()
        # The actual output dir for this run
        self._set_up_output_dir(pytest.config.getoption('test_dir'))
        # stdout and stderr from the run
        self.out = None
        self.err = None
        self.proc = None

    def _get_board_cfg_dir(self):
        this_dir = os.path.dirname(os.path.realpath(__file__))
        return os.path.join(this_dir, '../kicad5')

    def _get_board_name(self):
        self.board_file = os.path.join(self._get_board_cfg_dir(),
                                       self.prj_name,
                                       self.prj_name +
                                       (KICAD_PCB_EXT if self.mode == MODE_PCB else KICAD_SCH_EXT))
        logging.info('PCB file: '+self.board_file)
        assert os.path.isfile(self.board_file)

    def _set_up_output_dir(self, test_dir):
        if test_dir:
            self.output_dir = os.path.join(test_dir, self.test_name)
            os.makedirs(self.output_dir, exist_ok=True)
            self._del_dir_after = False
        else:
            # create a tmp dir
            self.output_dir = tempfile.mkdtemp(prefix='tmp-kicad_auto-'+self.test_name+'-')
            self._del_dir_after = True
        logging.info('Output dir: '+self.output_dir)

    def clean_up(self):
        logging.debug('Clean-up')
        if self._del_dir_after:
            logging.debug('Removing dir')
            shutil.rmtree(self.output_dir)

    def get_out_path(self, filename):
        return os.path.join(self.output_dir, filename)

    def expect_out_file(self, filename):
        file = self.get_out_path(filename)
        assert os.path.isfile(file)
        assert os.path.getsize(file) > 0
        return file

    def dont_expect_out_file(self, filename):
        file = self.get_out_path(filename)
        assert not os.path.isfile(file)

    def create_dummy_out_file(self, filename):
        file = self.get_out_path(filename)
        with open(file, 'w') as f:
            f.write('Dummy file\n')

    def get_pro_filename(self):
        return os.path.join(self._get_board_cfg_dir(), self.prj_name, self.prj_name+'.pro')

    def get_prodir_filename(self, file):
        return os.path.join(self._get_board_cfg_dir(), self.prj_name, file)

    def get_pro_mtime(self):
        return os.path.getmtime(self.get_pro_filename())

    def run(self, cmd, ret_val=None, extra=None, use_a_tty=False, filename=None):
        logging.debug('Running '+self.test_name)
        # Change the command to be local and add the board and output arguments
        cmd[0] = os.path.abspath(os.path.dirname(os.path.abspath(__file__))+'/../../src/'+cmd[0])
        cmd = [COVERAGE_SCRIPT, 'run', '-a']+cmd
        cmd.append(filename if filename else self.board_file)
        cmd.append(self.output_dir)
        if extra is not None:
            cmd = cmd+extra
        logging.debug(cmd)
        out_filename = self.get_out_path('output.txt')
        err_filename = self.get_out_path('error.txt')
        if use_a_tty:
            # This is used to test the coloured logs, we need stderr to be a TTY
            master, slave = openpty()
            f_err = slave
            f_out = slave
        else:
            # Redirect stdout and stderr to files
            f_out = os.open(out_filename, os.O_RDWR | os.O_CREAT)
            f_err = os.open(err_filename, os.O_RDWR | os.O_CREAT)
        # Run the process
        process = subprocess.Popen(cmd, stdout=f_out, stderr=f_err)
        ret_code = process.wait()
        logging.debug('ret_code '+str(ret_code))
        if use_a_tty:
            self.err = os.read(master, 10000)
            self.err = self.err.decode()
            self.out = self.err
        exp_ret = 0 if ret_val is None else ret_val
        assert ret_code == exp_ret
        if use_a_tty:
            os.close(master)
            os.close(slave)
            with open(out_filename, 'w') as f:
                f.write(self.out)
            with open(err_filename, 'w') as f:
                f.write(self.out)
        else:
            # Read stdout
            os.lseek(f_out, 0, os.SEEK_SET)
            self.out = os.read(f_out, 10000)
            os.close(f_out)
            self.out = self.out.decode()
            # Read stderr
            os.lseek(f_err, 0, os.SEEK_SET)
            self.err = os.read(f_err, 10000)
            os.close(f_err)
            self.err = self.err.decode()

    def search_out(self, text):
        m = re.search(text, self.out, re.MULTILINE)
        return m

    def search_err(self, text):
        m = re.search(text, self.err, re.MULTILINE)
        return m

    def search_in_file(self, file, texts):
        logging.debug('Searching in "'+file+'" output')
        with open(self.get_out_path(file)) as f:
            txt = f.read()
        for t in texts:
            logging.debug('- r"'+t+'"')
            m = re.search(t, txt, re.MULTILINE)
            assert m

    def compare_image(self, image, reference=None, diff='diff.png'):
        """ For images and single page PDFs """
        if reference is None:
            reference = image
        cmd = ['compare',
               # Tolerate 5 % error in color
               '-fuzz', '5%',
               # Count how many pixels differ
               '-metric', 'AE',
               self.get_out_path(image),
               os.path.join(REF_DIR, reference),
               # Avoid the part where KiCad version is printed
               '-crop', '100%x92%+0+0', '+repage',
               self.get_out_path(diff)]
        logging.debug('Comparing images with: '+str(cmd))
        res = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        # m = re.match(r'([\d\.e-]+) \(([\d\.e-]+)\)', res.decode())
        # assert m
        # logging.debug('MSE={} ({})'.format(m.group(1), m.group(2)))
        ae = int(res.decode())
        logging.debug('AE=%d' % ae)
        assert ae == 0

    def svg_to_png(self, svg):
        png = os.path.splitext(svg)[0]+'.png'
        logging.debug('Converting '+svg+' to '+png)
        cmd = ['convert', '-density', '150', svg, png]
        subprocess.check_call(cmd)
        return os.path.basename(png)

    def compare_svg(self, image, reference=None, diff='diff.png'):
        """ For SVGs, rendering to PNG """
        if reference is None:
            reference = image
        image_png = self.svg_to_png(self.get_out_path(image))
        reference_png = self.svg_to_png(os.path.join(REF_DIR, reference))
        self.compare_image(image_png, reference_png, diff)
        os.remove(os.path.join(REF_DIR, reference_png))

    def ps_to_png(self, ps):
        png = os.path.splitext(ps)[0]+'.png'
        logging.debug('Converting '+ps+' to '+png)
        cmd = ['convert', '-density', '150', ps, '-rotate', '90', png]
        subprocess.check_call(cmd)
        return os.path.basename(png)

    def compare_ps(self, image, reference=None, diff='diff.png'):
        """ For PSs, rendering to PNG """
        if reference is None:
            reference = image
        image_png = self.ps_to_png(self.get_out_path(image))
        reference_png = self.ps_to_png(os.path.join(REF_DIR, reference))
        self.compare_image(image_png, reference_png, diff)
        os.remove(os.path.join(REF_DIR, reference_png))

    def compare_pdf(self, gen, reference=None, diff='diff-{}.png'):
        """ For multi-page PDFs """
        if reference is None:
            reference = gen
        logging.debug('Comparing PDFs: '+gen+' vs '+reference)
        # Split the reference
        logging.debug('Splitting '+reference)
        cmd = ['convert', '-density', '150',
               os.path.join(REF_DIR, reference),
               self.get_out_path('ref-%d.png')]
        subprocess.check_call(cmd)
        # Split the generated
        logging.debug('Splitting '+gen)
        cmd = ['convert', '-density', '150',
               self.get_out_path(gen),
               self.get_out_path('gen-%d.png')]
        subprocess.check_call(cmd)
        # Chek number of pages
        ref_pages = glob(self.get_out_path('ref-*.png'))
        gen_pages = glob(self.get_out_path('gen-*.png'))
        logging.debug('Pages {} vs {}'.format(len(gen_pages), len(ref_pages)))
        assert len(ref_pages) == len(gen_pages)
        # Compare each page
        for page in range(len(ref_pages)):
            cmd = ['compare', '-metric', 'MSE',
                   self.get_out_path('ref-'+str(page)+'.png'),
                   self.get_out_path('gen-'+str(page)+'.png'),
                   # Avoid the part where KiCad version is printed
                   '-crop', '100%x92%+0+0', '+repage',
                   self.get_out_path(diff.format(page))]
            logging.debug('Comparing images with: '+str(cmd))
            res = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            m = re.match(r'([\d\.]+) \(([\d\.]+)\)', res.decode())
            assert m
            logging.debug('MSE={} ({})'.format(m.group(1), m.group(2)))
            assert float(m.group(2)) == 0.0

    def compare_txt(self, text, reference=None, diff='diff.txt'):
        if reference is None:
            reference = text
        cmd = ['/bin/sh', '-c', 'diff -ub '+os.path.join(REF_DIR, reference)+' ' +
               self.get_out_path(text)+' > '+self.get_out_path(diff)]
        logging.debug('Comparing texts with: '+str(cmd))
        res = subprocess.call(cmd)
        assert res == 0

    def filter_txt(self, file, pattern, repl):
        fname = self.get_out_path(file)
        with open(fname) as f:
            txt = f.read()
        with open(fname, 'w') as f:
            f.write(re.sub(pattern, repl, txt))

    @contextmanager
    def start_kicad(self, cmd):
        """ Context manager to run a command under a virual X server.
            Use like this: with context.start_kicad('command'): """
        xvfb_kwargs = {'width': 800, 'height': 600, 'colordepth': 24, }
        with recorded_xvfb(None, None, False, False, **xvfb_kwargs):
            with PopenContext([cmd], stderr=subprocess.DEVNULL, close_fds=True) as self.proc:
                logging.debug('Started '+cmd+' with PID: '+str(self.proc.pid))
                assert pid_exists(self.proc.pid)
                yield

    def stop_kicad(self):
        if self.proc:
            self.proc.terminate()
            self.proc = None


class TestContextSCH(TestContext):

    def __init__(self, test_name, prj_name):
        super().__init__(test_name, prj_name)
        self.mode = MODE_SCH
        self._get_board_name()
