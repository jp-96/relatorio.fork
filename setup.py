import codecs
import os
import re

from setuptools import find_packages, setup


def read(fname):
    return codecs.open(
        os.path.join(os.path.dirname(__file__), fname), 'r', 'utf-8').read()


def get_version():
    init = open(os.path.join(os.path.dirname(__file__), 'relatorio',
                             '__init__.py')).read()
    return re.search(r"""__version__ = '([0-9.]*)'""", init).group(1)


setup(
    name="relatorio",
    description="A templating library able to output odt and pdf files",
    long_description=read('README.rst'),
    author='Tryton',
    author_email='relatorio@tryton.org',
    url='https://pypi.python.org/pypi/relatorio',
    download_url='https://downloads.tryton.org/relatorio/',
    project_urls={
        "Bug Tracker": 'https://bugs.tryton.org/relatorio',
        "Documentation": 'https://docs.tryton.org/relatorio',
        "Forum": 'https://discuss.tryton.org/tags/relatorio',
        "Source Code": 'https://code.tryton.org/relatorio',
        },
    keywords='templating OpenDocument PDF',
    license="GPL License",
    version=get_version() + "+jp96",
    packages=find_packages(exclude=['examples']),
    package_data={
        'relatorio.tests': [
            '*.jpg', '*.odt', '*.fodt', '*.png', 'templates/*.tmpl'],
        },
    scripts=['scripts/relatorio-render'],
    python_requires='>=3.5',
    install_requires=[
        "Genshi >= 0.5",
        "lxml >= 2.0"
    ],
    extras_require={
        'chart': [
            'pycha @ git+https://github.com/BonaFideIT/pycha3.git',
            'pyyaml == 5.3.1',
            'pycairo'
        ],
        'fodt': [
            'python-magic'
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU General Public License (GPL)",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Text Processing",
        ],
    )
