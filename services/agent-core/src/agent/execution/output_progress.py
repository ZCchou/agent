"""In-memory digest-only observation of sustained periodic output, not a generation cap."""
from collections import Counter,deque
from hashlib import sha256

class OutputProgress:
    def __init__(self):
        self.pending=sha256();self.characters=0;self.blocks=deque();self.counts=Counter();self.recent=deque(maxlen=32)
    def feed(self,text):
        fresh=False
        for character in text:
            self.pending.update(character.encode('utf-8'));self.characters+=1
            if self.characters==32:
                key=self.pending.digest();self.pending=sha256();self.characters=0
                fresh=fresh or self.counts[key]<3
                self.recent.append(self.counts[key]>=3)
                self.blocks.append(key);self.counts[key]+=1
                if len(self.blocks)>256:
                    old=self.blocks.popleft();self.counts[old]-=1
                    if not self.counts[old]:del self.counts[old]
        return fresh or not (len(self.recent)==32 and sum(self.recent)>=30)
