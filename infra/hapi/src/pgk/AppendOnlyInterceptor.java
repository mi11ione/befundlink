package pgk;

import ca.uhn.fhir.interceptor.api.Hook;
import ca.uhn.fhir.interceptor.api.Interceptor;
import ca.uhn.fhir.interceptor.api.Pointcut;
import ca.uhn.fhir.rest.server.exceptions.MethodNotAllowedException;
import org.hl7.fhir.instance.model.api.IBaseResource;

/**
 * Audit integrity for the Befundlink HAPI server.
 * - AuditEvent is create-only: any update (PUT, PATCH, also inside a transaction) is rejected with 405.
 * - Any delete is rejected with 405 (defence in depth; hapi.fhir.delete_enabled=false already blocks DELETE).
 * Storage pointcuts fire for plain REST calls and for every entry of a batch/transaction Bundle.
 * Registered via hapi.fhir.custom-interceptor-classes; loaded from /app/extra-classes.
 */
@Interceptor
public class AppendOnlyInterceptor {

    @Hook(Pointcut.STORAGE_PRESTORAGE_RESOURCE_UPDATED)
    public void onUpdate(IBaseResource oldResource, IBaseResource newResource) {
        if ("AuditEvent".equals(oldResource.fhirType()) || "AuditEvent".equals(newResource.fhirType())) {
            throw new MethodNotAllowedException("AuditEvent is append-only: updates are not allowed");
        }
    }

    @Hook(Pointcut.STORAGE_PRESTORAGE_RESOURCE_DELETED)
    public void onDelete(IBaseResource resource) {
        throw new MethodNotAllowedException("This server is append-only: " + resource.fhirType() + " cannot be deleted");
    }
}
