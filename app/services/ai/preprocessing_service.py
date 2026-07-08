import re
import unicodedata


class PreprocessingService:
    """
        Handles normalization of Job Description text before 
        any AI processing.
    """
    
    def normalize(self, text: str)-> str:
        if not text:
            return ""
        
        
        #normalize unicode characters
        text = unicodedata.normalize("NFKC", text)    # Converts weird pasted characters into standard Unicode.
        
        
        #Convert Windows/Mac line endings to Unix line endings
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        
        #replace tabs with spaces
        text = text.replace("\t", " ")
        
        
        # remove common bullet symbols from the text
        text = re.sub(r"[\u2022\u2023\u25E6\u2043\u2219]", "", text)
        
        #collapse multiple spaces
        text = re.sub(r"[ ]{2,}", " ", text)
        
        #collapse multiple blank lines
        text = re.sub(r"\n+", "\n", text)
        
        #Trim each line
        lines = [line.strip() for line in text.splitlines()]
        
        #remove empty lines at beginning and end of text
        text = "\n".join(lines).strip()
        
        #convert to lowercase
        text = text.lower()
        
        return text
    
    
        
          