from typing import Any, Dict, List, Optional, Tuple, Union, Iterable, TextIO
from pathlib import Path
import logging
import sys

class SeanergysLogger(logging.Logger):

    def __init__(self, name: str = "Senergys logs", level: Union[str,int] = logging.INFO, out: Union[TextIO,str,Path] = sys.stdout):    
        super().__init__(name, level)
        handler = logging.FileHandler(out) if isinstance(out, str) or isinstance(out, Path) else logging.StreamHandler(out) 
        handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
        handler.setLevel(logging.INFO)
        self.addHandler(handler)
    
    # def _setup_logger(self):
    #     """Setup a default logger."""
    #     logger = logging.getLogger(self.__class__.__name__)
    #     if not logger.handlers:
    #         handler = logging.StreamHandler()
    #         formatter = logging.Formatter(
    #             '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    #         )
    #         handler.setFormatter(formatter)
    #         logger.addHandler(handler)
    #         logger.setLevel(logging.INFO)
    #     return logger
