import chromadb
import google.generativeai as genai
import os
from dotenv import load_dotenv
from logs.logger import logger

class MemoryManager:
    """Handles the agent's memory operations using ChromaDB and Gemini embeddings."""

    def __init__(self, chroma_path="data/chroma_db", collection_name="cognito_memory"):
        """
        Initializes the MemoryManager.

        Args:
            chroma_path (str): Path to the directory where ChromaDB stores its data.
            collection_name (str): Name of the collection to use for memories.
        """
        # Load API key for the embedding model
        load_dotenv()
        if not os.getenv("GEMINI_API_KEY"):
            raise ValueError("GEMINI_API_KEY not found in .env file for MemoryManager.")

        # Initialize the ChromaDB client and collection
        self.client = chromadb.PersistentClient(path=chroma_path)
        self.collection = self.client.get_or_create_collection(name=collection_name)
        
        # Initialize the Gemini embedding model
        # text-embedding-004 is a powerful and efficient model for this task.
        self.embedding_model = 'models/gemini-embedding-001'
        logger.info("MemoryManager initialized successfully.")

    def add_memory(self, document: str, doc_id: str, metadata: dict = None):
        """
        Adds a single document (memory) to the collection.

        Args:
            document (str): The text content of the memory.
            doc_id (str): A unique ID for the memory.
            metadata (dict, optional): A dictionary of metadata. Defaults to None.
        """
        try:
            # Note: ChromaDB's add method can handle embedding for you,
            # but doing it explicitly gives us more control.
            self.collection.add(
                documents=[document],
                metadatas=[metadata] if metadata else [{}],
                ids=[doc_id]
            )
            logger.info(f"-> Added memory with ID: {doc_id}")
        except Exception as e:
            logger.error(f"ERROR: Failed to add memory '{doc_id}': {e}")

    def find_similar_memories(self, query_text: str, n_results: int = 3) -> list:
        """
        Finds memories in the collection that are similar to the query text.

        Args:
            query_text (str): The text to search for.
            n_results (int): The number of similar memories to return.

        Returns:
            list: A list of the most similar documents found.
        """
        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=n_results
            )
            return results.get('documents', [[]])[0]
        except Exception as e:
            logger.error(f"ERROR: Failed to query memories: {e}")
            return []

            # Add this code to the VERY END of your memory_manager.py file

if __name__ == '__main__':
    logger.info("\n--- Testing MemoryManager ---")
    
    # Initialize the manager
    memory = MemoryManager()
    
    # Create a unique ID and document for this test run
    import uuid
    test_id = f"test_{uuid.uuid4()}"
    test_doc = "The user's favorite color is blue."
    
    # 1. Add a new memory
    logger.info("\n--- Adding a test memory ---")
    memory.add_memory(
        document=test_doc,
        doc_id=test_id,
        metadata={"source": "test_run"}
    )
    
    # 2. Query for a similar concept
    logger.info("\n--- Querying for a similar memory ---")
    query = "What does the user like?"
    similar_memories = memory.find_similar_memories(query, n_results=1)
    
    logger.info(f"\nQuery: '{query}'")
    logger.info(f"Found memories: {similar_memories}")
    
    # 3. Verify the result
    if similar_memories and similar_memories[0] == test_doc:
        logger.info("\nSUCCESS: The correct memory was retrieved.")
    else:
        logger.error("\nFAILURE: Did not retrieve the expected memory.")