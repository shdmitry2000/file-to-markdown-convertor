create project that enables REST API endpoint
that converts file on folder like pdf,docx to markdown 

the client will sent REST API call to process file like 'POST http://this-application/convert' with path parameter
it will add to zero-mq list
the parallel background workers based on cpu cores will proceses by zero-mq queue fifo
the workers will convert file with docling like in @.tmp-old-files/02-MDDocumentIndexer.py
and will save it to same path structure but in convertd folder parent

and the endpoint will return generated `convertion id`

there is also endpoint to check the status of the item that will be processed or processing like 'GET http://this-application/convert/<convertion id>'

there is also endpoint to get the convertd file like 'GET http://this-application/converted/<file path>' and it will get from local folder where is converted


tech stack:
 - package manager: uv
 - use fastapi

